"""BITC stamper.

Reads a relay path, computes the BITC/Unix time per frame (the ONE clock —
rolling-min of wall-minus-rtp), burns the BITC marker into every video frame,
re-encodes, and republishes to ``<src>-stamped``. Audio is remuxed (copied)
through. Downstream consumers (recorder, detector) read the time from the
pixels instead of running their own clock — no cross-consumer reconciliation.

Reads from the relay (free fan-out), never the camera directly.
"""

from __future__ import annotations

import collections
import logging
import os
import re
import threading
import time
from fractions import Fraction

import numpy as np

from . import bitc
from .live_detector import _list_relay_paths
from .media_health import MediaHealthStore, default_db_path

LOG = logging.getLogger("wanyard.stamper")

_ROLLING_SECONDS = 90.0
_DISC_BACKWARD = -1.0      # rtp step below this (s) = discontinuity → reanchor
_DISC_FORWARD = 5.0        # rtp step above this (s) = discontinuity → reanchor
_PATH_REFRESH_SECONDS = 30.0
_STAMPED_SUFFIX = "-stamped"

_nvenc_probe: dict[str, bool] = {}


def _nvenc_available(codec: str = "h264_nvenc") -> bool:
    """True if ``codec`` (h264_nvenc/hevc_nvenc) can actually open + encode here.

    Cached per codec: the codec is compiled into ffmpeg regardless of hardware
    (and a container started without gpu.yml has no GPU at all), so presence is
    not enough — open a tiny encoder and push one frame; that fails without a
    usable NVIDIA GPU, which is exactly the signal we want.
    """
    if codec in _nvenc_probe:
        return _nvenc_probe[codec]
    ok = False
    try:
        import av

        # 1280x720, not a tiny frame: NVENC rejects sub-minimum dimensions with
        # EINVAL (a 64x64 probe gives a false negative — the encoder is fine).
        cc = av.codec.CodecContext.create(codec, "w")
        cc.width = 1280
        cc.height = 720
        cc.pix_fmt = "yuv420p"
        cc.time_base = Fraction(1, 30)
        cc.open()
        cc.encode(av.VideoFrame(1280, 720, "yuv420p"))
        cc.encode(None)
        ok = True
    except Exception as exc:
        LOG.info("%s unavailable: %s", codec, exc)
    _nvenc_probe[codec] = ok
    return ok


class _StampAnchor:
    """abs_ts = rtp + delta, delta = min(wall - rtp) over a rolling window.

    Usable from frame 1 (no startup gate — the stamper must always emit), the
    min settling toward the true arrival floor as samples accumulate and
    tracking the camera's clock drift. Resets on an RTP discontinuity.
    """

    def __init__(self, source_id: str) -> None:
        self.source_id = source_id
        self.samples: collections.deque[tuple[float, float]] = collections.deque()
        self.last_rtp: float | None = None

    def observe(self, rtp: float, wall: float) -> float:
        if self.last_rtp is not None:
            step = rtp - self.last_rtp
            if step < _DISC_BACKWARD or step > _DISC_FORWARD:
                self.samples.clear()
                LOG.info("stamper %s rtp discontinuity %.3fs — reanchor",
                         self.source_id, step)
        self.last_rtp = rtp
        self.samples.append((wall, wall - rtp))
        cutoff = wall - _ROLLING_SECONDS
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        delta = min(d for _, d in self.samples)
        return rtp + delta


class _StamperWorker:
    def __init__(self, relay_host: str, source_id: str,
                 stop_event: threading.Event, out_host: str | None = None,
                 health_store: MediaHealthStore | None = None) -> None:
        self.relay_host = relay_host
        self.source_id = source_id
        self.stop_event = stop_event
        self.local_stop = threading.Event()
        self.in_url = f"rtsp://{relay_host}:8554/{source_id}"
        self.out_url = f"rtsp://{out_host or relay_host}:8554/{source_id}{_STAMPED_SUFFIX}"
        # Every knob takes a per-source override (cameras differ in native
        # bitrate, so a single global cap is a compromise): WANYARD_STAMP_<SRC>_
        # <KEY> wins over the global WANYARD_STAMP_<KEY>, where <SRC> is the
        # source_id upper-cased with non-alnum -> _ (tapo-front -> TAPO_FRONT).
        # Encoder: "auto" prefers h264_nvenc when the GPU probe succeeds, else
        # libx264 (no hard GPU dependency — non-NVIDIA boxes fall back cleanly).
        self.encoder = self._src_env("ENCODER", "auto")
        # Quality targets (per encoder). x264 uses crf; nvenc uses cq. Both are
        # quality-targeted so easy/static scenes stay lean and only motion costs
        # bits — mirrors the camera's own rate control.
        self.crf = self._src_env("CRF", "23")
        self.cq = self._src_env("CQ", "25")
        # libx264 default veryfast, not ultrafast: ultrafast re-encodes
        # already-compressed CCTV so inefficiently it looks like garbage even at
        # native bitrate. nvenc has its own preset knob.
        self.preset = self._src_env("PRESET", "veryfast")
        self.nvenc_preset = self._src_env("NVENC_PRESET", "p5")
        # VBV cap above camera native (re-encoding compressed video needs
        # headroom to match the original's look). crf/cq keep the average near
        # native; maxrate just bounds motion peaks for predictable retention.
        # BITC marker is bitrate-independent (solid 8px luma cells decode ~99%
        # even at 0.85 Mbps).
        self.maxrate = self._src_env("MAXRATE")
        self.bufsize = self._src_env("BUFSIZE")
        # Runtime encoder fallback. The startup NVENC probe can pass (it grabs a
        # momentary session) while the real per-camera open later fails — e.g. the
        # GPU's concurrent-NVENC-session cap is already full. When that happens we
        # demote this camera's encoder for the rest of the process instead of
        # looping forever on a codec that can't open.
        self._codec_override: str | None = None
        self._active_codec: str | None = None
        self._health_store = health_store
        self._reconnect_count = 0
        self._fallback_count = 0
        self._reported_encoder: str | None = None
        self._last_reconnect_event_ts = 0.0
        self._reported_recovery_count = 0
        self.thread = threading.Thread(
            target=self.run, daemon=True, name=f"stamper-{source_id}")

    def _src_env(self, key: str, default: str | None = None) -> str | None:
        """Per-source env with global fallback: WANYARD_STAMP_<SRC>_<KEY>."""
        tok = re.sub(r"[^A-Z0-9]+", "_", self.source_id.upper())
        return os.environ.get(
            f"WANYARD_STAMP_{tok}_{key}",
            os.environ.get(f"WANYARD_STAMP_{key}", default),
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.local_stop.set()
        self._health_state("stopped")

    def stopped(self) -> bool:
        return self.stop_event.is_set() or self.local_stop.is_set()

    def run(self) -> None:
        backoff = 1.0
        self._health_state("starting")
        while not self.stopped():
            try:
                self._stream()
                backoff = 1.0
            except Exception as exc:
                if self._demote_on_encoder_error(exc):
                    self._wait(1.0)       # retry promptly with the fallback encoder
                    backoff = 1.0
                    continue
                self._reconnect_count += 1
                self._health_state("reconnecting", last_error=str(exc))
                now = time.time()
                if now - self._last_reconnect_event_ts >= 300:
                    self._health_event(
                        "warning", "reconnect",
                        f"stream reconnect in {backoff:.1f}s: {exc}",
                        {
                            "backoff_seconds": backoff,
                            "reconnect_count": self._reconnect_count,
                        },
                    )
                    self._last_reconnect_event_ts = now
                LOG.warning("stamper %s reconnect in %.1fs after %s",
                            self.source_id, backoff, exc)
                self._wait(backoff)
                backoff = min(30.0, backoff * 2.0)
        self._health_state("stopped")
        LOG.info("stamper %s stopped", self.source_id)

    def _demote_on_encoder_error(self, exc: Exception) -> bool:
        """On a failure to OPEN the video encoder (e.g. NVENC session cap hit at
        runtime), fall back hevc_nvenc → h264_nvenc → libx264 for the rest of the
        process so the camera stays alive (CPU) instead of looping on a codec that
        can't open. Non-open errors (network etc.) take the normal reconnect."""
        demote = {"hevc_nvenc": "h264_nvenc", "h264_nvenc": "libx264"}
        nxt = demote.get(self._active_codec or "")
        if nxt and "avcodec_open2" in str(exc):
            LOG.warning("stamper %s encoder %s failed to open — falling back to %s",
                        self.source_id, self._active_codec, nxt)
            old = self._active_codec
            self._codec_override = nxt
            self._fallback_count += 1
            self._health_state("fallback", active_encoder=nxt, last_error=str(exc))
            self._health_event(
                "warning", "encoder_fallback",
                f"encoder {old} failed to open; falling back to {nxt}",
                {"from": old, "to": nxt, "error": str(exc)},
            )
            return True
        return False

    def _health_state(
        self,
        status: str,
        *,
        active_encoder: str | None = None,
        width: int | None = None,
        height: int | None = None,
        fps: float | None = None,
        maxrate: str | None = None,
        bufsize: str | None = None,
        last_error: str | None = None,
    ) -> None:
        if self._health_store is None:
            return
        try:
            self._health_store.update_stamper_state(
                self.source_id,
                status=status,
                active_encoder=active_encoder or self._active_codec,
                width=width,
                height=height,
                fps=fps,
                maxrate=maxrate,
                bufsize=bufsize,
                reconnect_count=self._reconnect_count,
                fallback_count=self._fallback_count,
                last_error=last_error,
            )
        except Exception:
            LOG.warning("stamper %s could not update media health", self.source_id,
                        exc_info=True)

    def _health_event(
        self, severity: str, kind: str, message: str, details: dict | None = None
    ) -> None:
        if self._health_store is None:
            return
        try:
            self._health_store.event(
                self.source_id, "stamper", severity, kind, message, details
            )
        except Exception:
            LOG.warning("stamper %s could not record media health event",
                        self.source_id, exc_info=True)

    def _wait(self, seconds: float) -> None:
        end = time.time() + seconds
        while not self.stopped() and time.time() < end:
            time.sleep(min(0.25, max(0.0, end - time.time())))

    def _video_codec(self) -> str:
        """Resolve the output video encoder.

        ``auto`` prefers GPU and best codec: hevc_nvenc → h264_nvenc → libx264,
        each gated by a real open+encode probe (the codecs are compiled in even
        with no GPU / a container started without gpu.yml, so presence is not
        enough). A GPU-less box (the default `docker compose up`) lands on
        libx264 — no crash loop.
        """
        if self._codec_override:
            return self._codec_override   # runtime fallback after an open failure
        if self.encoder == "libx264":
            return "libx264"
        if self.encoder in ("nvenc", "h264_nvenc"):
            return "h264_nvenc"
        if self.encoder in ("hevc", "hevc_nvenc", "h265_nvenc"):
            return "hevc_nvenc"
        for cand in ("hevc_nvenc", "h264_nvenc"):
            if _nvenc_available(cand):
                LOG.info("stamper %s encoder=auto resolved to %s", self.source_id, cand)
                return cand
        codec = "libx264"
        LOG.info("stamper %s encoder=auto resolved to %s (no usable NVENC)",
                 self.source_id, codec)
        return codec

    def _video_options(self, codec: str, gop: int) -> dict[str, str]:
        opts = {"g": str(gop), "keyint_min": str(gop)}
        if codec.endswith("_nvenc"):
            # Low-latency tuning is mandatory, not optional: the live detector
            # drops frames whose age exceeds 1.0s (_MAX_LIVE_LAG_SECONDS), and
            # NVENC's default lookahead/output-delay buffering pushes end-to-end
            # latency past that, starving detection entirely. tune=ll + no
            # B-frames / lookahead / output delay keeps latency under the gate.
            # Quality comes from cq + bitrate, not from B-frames, so the look is
            # unaffected.
            opts.update({"preset": self.nvenc_preset, "tune": "ll", "rc": "vbr",
                         "cq": self.cq, "bf": "0", "rc-lookahead": "0",
                         "delay": "0"})
        else:
            # zerolatency: same reason — keep the re-encode within the live
            # detector's 1.0s staleness gate.
            opts.update({"preset": self.preset, "tune": "zerolatency",
                         "crf": self.crf})
        family = "HEVC" if codec == "hevc_nvenc" else "H264"
        maxrate = self._src_env(f"{family}_MAXRATE", self.maxrate)
        bufsize = self._src_env(f"{family}_BUFSIZE", self.bufsize)
        if maxrate:
            opts["maxrate"] = maxrate
            opts["bufsize"] = bufsize or maxrate
        return opts

    def _stream(self) -> None:
        import av

        LOG.info("stamper %s %s -> %s", self.source_id, self.in_url, self.out_url)
        self._health_state("connecting")
        inp = av.open(self.in_url, options={"rtsp_transport": "tcp"}, timeout=(5.0, 5.0))
        out = None
        try:
            vin = next(s for s in inp.streams if s.type == "video")
            ain = next((s for s in inp.streams if s.type == "audio"), None)

            out = av.open(self.out_url, "w", format="rtsp",
                          options={"rtsp_transport": "tcp"})
            codec = self._video_codec()
            self._active_codec = codec
            vout = out.add_stream(codec, rate=vin.average_rate or 15)
            vout.width = vin.codec_context.width
            vout.height = vin.codec_context.height
            vout.pix_fmt = "yuv420p"
            # Fixed 90kHz output timebase; assign pts from the input's RTP
            # (session-relative, monotonic, real-rate) so playback timing mirrors
            # the camera exactly and the RTSP muxer always gets clean monotonic
            # pts (carrying input frame.pts through a mismatched encoder rate
            # EINVALs for VFR streams, e.g. avg 15 != base 20).
            out_tb = Fraction(1, 90000)
            vout.codec_context.time_base = out_tb
            # Keyframe interval. HLS segments can only break on keyframes, so the
            # GOP is the floor on live segment duration (and thus live latency).
            # Default 2s (seekable records); set WANYARD_STAMP_GOP_SECONDS=1 for
            # lower-latency live (more I-frames, slightly fatter).
            gop_seconds = float(self._src_env("GOP_SECONDS", "2"))
            gop = max(1, int(round(gop_seconds * float(vin.average_rate or 15))))
            video_options = self._video_options(codec, gop)
            vout.options = video_options
            fps = float(vin.average_rate or 15)
            self._health_state(
                "connecting",
                active_encoder=codec,
                width=vin.codec_context.width,
                height=vin.codec_context.height,
                fps=fps,
                maxrate=video_options.get("maxrate"),
                bufsize=video_options.get("bufsize"),
            )
            if codec != self._reported_encoder:
                self._health_event(
                    "info",
                    "encoder_selected",
                    f"encoder selected: {codec}",
                    {
                        "encoder": codec,
                        "width": vin.codec_context.width,
                        "height": vin.codec_context.height,
                        "fps": fps,
                        "maxrate": video_options.get("maxrate"),
                        "bufsize": video_options.get("bufsize"),
                    },
                )
                self._reported_encoder = codec
            aout = None
            if ain is not None:
                rate = getattr(ain.codec_context, "sample_rate", None) or 8000
                for attempt in ("explicit", "template"):
                    try:
                        if attempt == "explicit":
                            aout = out.add_stream(ain.codec_context.name, rate=rate)
                            try:
                                aout.layout = ain.layout
                            except Exception:
                                pass
                        else:
                            aout = out.add_stream(template=ain)
                        break
                    except Exception as exc:
                        LOG.info("stamper %s audio add_stream(%s) failed: %s",
                                 self.source_id, attempt, exc)
                        aout = None
                if aout is None:
                    LOG.warning("stamper %s audio unsupported — video only",
                                self.source_id)

            anchor = _StampAnchor(self.source_id)
            rtp0: float | None = None
            last_pts = -1
            last_health_update = 0.0
            for packet in inp.demux():
                if self.stopped():
                    break
                if packet.dts is None:
                    continue
                if packet.stream is vin:
                    rtp = (float(packet.pts * vin.time_base)
                           if packet.pts is not None else time.time())
                    for frame in packet.decode():
                        wall = time.time()
                        abs_ts = anchor.observe(rtp, wall)
                        if rtp0 is None:
                            rtp0 = rtp
                        # Stamp directly into the decoded yuv420p planes: the
                        # marker is pure luma, so write the Y cells and
                        # neutralise chroma in place. Avoids the bgr24
                        # round-trip (two full-frame colourspace conversions
                        # per frame just to touch a luma strip).
                        if frame.format.name != "yuv420p":
                            frame = frame.reformat(format="yuv420p")
                        try:
                            value = bitc.encode_value(abs_ts)
                        except ValueError:
                            LOG.warning("stamper %s abs_ts %.3f out of range",
                                        self.source_id, abs_ts)
                        else:
                            yb, ub, vb = frame.planes
                            y = np.frombuffer(yb, np.uint8).reshape(-1, yb.line_size)
                            u = np.frombuffer(ub, np.uint8).reshape(-1, ub.line_size)
                            vp = np.frombuffer(vb, np.uint8).reshape(-1, vb.line_size)
                            bitc.render_yuv420(y, u, vp, value)
                        pts = int(round((rtp - rtp0) * 90000))
                        frame.pts = pts if pts > last_pts else last_pts + 1
                        last_pts = frame.pts
                        frame.time_base = out_tb
                        encoded = vout.encode(frame)
                        for op in encoded:
                            out.mux(op)
                        if wall - last_health_update >= 15.0:
                            self._health_state(
                                "live",
                                active_encoder=codec,
                                width=vout.width,
                                height=vout.height,
                                fps=fps,
                                maxrate=video_options.get("maxrate"),
                                bufsize=video_options.get("bufsize"),
                            )
                            if self._reconnect_count > self._reported_recovery_count:
                                self._health_event(
                                    "info",
                                    "stream_recovered",
                                    f"stream recovered after {self._reconnect_count} reconnects",
                                    {"reconnect_count": self._reconnect_count},
                                )
                                self._reported_recovery_count = self._reconnect_count
                            last_health_update = wall
                elif aout is not None and packet.stream is ain:
                    packet.stream = aout
                    try:
                        out.mux(packet)
                    except Exception as exc:
                        LOG.info("stamper %s audio mux dropped: %s",
                                 self.source_id, exc)
            if not self.stopped():
                for op in vout.encode():
                    out.mux(op)
                raise EOFError("rtsp input ended")
        finally:
            if out is not None:
                try:
                    out.close()
                except Exception:
                    pass
            inp.close()


class _StamperSupervisor:
    def __init__(self, stop_event: threading.Event) -> None:
        self.stop_event = stop_event
        self.relay_host = os.environ.get("WANYARD_RELAY_HOST", "mediamtx").strip() or "mediamtx"
        self.workers: dict[str, _StamperWorker] = {}
        self.health_store: MediaHealthStore | None = None
        try:
            # Telemetry is best-effort and runs on the frame thread. Bound any
            # SQLite writer contention so monitoring cannot stall the stream.
            self.health_store = MediaHealthStore(default_db_path(), timeout=0.05)
        except Exception:
            LOG.warning("stamper media health database unavailable", exc_info=True)
        self.thread = threading.Thread(
            target=self.run, daemon=True, name="stamper-supervisor")

    def start(self) -> threading.Thread:
        LOG.info("stamper supervisor starting relay=%s", self.relay_host)
        self.thread.start()
        return self.thread

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._sync_paths()
            except Exception:
                LOG.exception("stamper path discovery failed")
            self.stop_event.wait(_PATH_REFRESH_SECONDS)
        for worker in self.workers.values():
            worker.stop()
        LOG.info("stamper supervisor stopped")

    def _sync_paths(self) -> None:
        # Source paths only — never stamp our own output (would loop).
        paths = {p for p in _list_relay_paths(self.relay_host)
                 if not p.endswith(_STAMPED_SUFFIX)}
        for source_id in sorted(paths - set(self.workers)):
            worker = _StamperWorker(
                self.relay_host,
                source_id,
                self.stop_event,
                health_store=self.health_store,
            )
            self.workers[source_id] = worker
            worker.start()
            LOG.info("stamper started path=%s", source_id)
        for source_id in sorted(set(self.workers) - paths):
            self.workers[source_id].stop()
            del self.workers[source_id]
            LOG.info("stamper stopped removed path=%s", source_id)


def run() -> int:
    import signal

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    stop_event = threading.Event()

    def _shutdown(sig, frame):
        LOG.info("stamper shutting down")
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    sup = _StamperSupervisor(stop_event)
    sup.start()
    while not stop_event.is_set():
        time.sleep(1.0)
    sup.thread.join(timeout=10.0)
    return 0
