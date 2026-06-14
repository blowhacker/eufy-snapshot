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
import threading
import time
from fractions import Fraction

import numpy as np

from . import bitc
from .live_detector import _list_relay_paths

LOG = logging.getLogger("wanyard.stamper")

_ROLLING_SECONDS = 90.0
_DISC_BACKWARD = -1.0      # rtp step below this (s) = discontinuity → reanchor
_DISC_FORWARD = 5.0        # rtp step above this (s) = discontinuity → reanchor
_PATH_REFRESH_SECONDS = 30.0
_STAMPED_SUFFIX = "-stamped"

_nvenc_probe: bool | None = None


def _nvenc_available() -> bool:
    """True if h264_nvenc can actually open + encode on this host.

    Cached: the codec is compiled into ffmpeg regardless of hardware, so we
    open a tiny encoder and push one frame — that fails without a real NVIDIA
    GPU, which is exactly the signal we want.
    """
    global _nvenc_probe
    if _nvenc_probe is not None:
        return _nvenc_probe
    try:
        import av

        # 1280x720, not a tiny frame: NVENC rejects sub-minimum dimensions with
        # EINVAL (a 64x64 probe gives a false negative — the encoder is fine).
        cc = av.codec.CodecContext.create("h264_nvenc", "w")
        cc.width = 1280
        cc.height = 720
        cc.pix_fmt = "yuv420p"
        cc.time_base = Fraction(1, 30)
        cc.open()
        cc.encode(av.VideoFrame(1280, 720, "yuv420p"))
        cc.encode(None)
        _nvenc_probe = True
    except Exception as exc:
        LOG.info("h264_nvenc unavailable, using libx264: %s", exc)
        _nvenc_probe = False
    return _nvenc_probe


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
                 stop_event: threading.Event, out_host: str | None = None) -> None:
        self.relay_host = relay_host
        self.source_id = source_id
        self.stop_event = stop_event
        self.local_stop = threading.Event()
        self.in_url = f"rtsp://{relay_host}:8554/{source_id}"
        self.out_url = f"rtsp://{out_host or relay_host}:8554/{source_id}{_STAMPED_SUFFIX}"
        # Encoder: "auto" prefers h264_nvenc when the GPU probe succeeds, else
        # libx264 (no hard GPU dependency — non-NVIDIA boxes fall back cleanly).
        self.encoder = os.environ.get("WANYARD_STAMP_ENCODER", "auto")
        # Quality targets (per encoder). x264 uses crf; nvenc uses cq. Both are
        # quality-targeted so easy/static scenes stay lean and only motion costs
        # bits — mirrors the camera's own rate control.
        self.crf = os.environ.get("WANYARD_STAMP_CRF", "23")
        self.cq = os.environ.get("WANYARD_STAMP_CQ", "25")
        # libx264 default veryfast, not ultrafast: ultrafast re-encodes
        # already-compressed CCTV so inefficiently it looks like garbage even at
        # native bitrate. nvenc has its own preset knob.
        self.preset = os.environ.get("WANYARD_STAMP_PRESET", "veryfast")
        self.nvenc_preset = os.environ.get("WANYARD_STAMP_NVENC_PRESET", "p5")
        # VBV cap above camera native (re-encoding compressed video needs
        # headroom to match the original's look). crf/cq keep the average near
        # native; maxrate just bounds motion peaks for predictable retention.
        # BITC marker is bitrate-independent (solid 8px luma cells decode ~99%
        # even at 0.85 Mbps).
        self.maxrate = os.environ.get("WANYARD_STAMP_MAXRATE")
        self.bufsize = os.environ.get("WANYARD_STAMP_BUFSIZE")
        self.thread = threading.Thread(
            target=self.run, daemon=True, name=f"stamper-{source_id}")

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.local_stop.set()

    def stopped(self) -> bool:
        return self.stop_event.is_set() or self.local_stop.is_set()

    def run(self) -> None:
        backoff = 1.0
        while not self.stopped():
            try:
                self._stream()
                backoff = 1.0
            except Exception as exc:
                LOG.warning("stamper %s reconnect in %.1fs after %s",
                            self.source_id, backoff, exc)
                self._wait(backoff)
                backoff = min(30.0, backoff * 2.0)
        LOG.info("stamper %s stopped", self.source_id)

    def _wait(self, seconds: float) -> None:
        end = time.time() + seconds
        while not self.stopped() and time.time() < end:
            time.sleep(min(0.25, max(0.0, end - time.time())))

    def _video_codec(self) -> str:
        """Resolve the output video encoder.

        ``auto`` uses h264_nvenc when a real GPU encode probe succeeds (the
        codec is compiled in even on non-NVIDIA hosts, so presence alone is not
        enough — we must actually open it), otherwise libx264.
        """
        if self.encoder == "libx264":
            return "libx264"
        if self.encoder == "nvenc":
            return "h264_nvenc"
        codec = "h264_nvenc" if _nvenc_available() else "libx264"
        LOG.info("stamper %s encoder=auto resolved to %s", self.source_id, codec)
        return codec

    def _video_options(self, codec: str, gop: int) -> dict[str, str]:
        opts = {"g": str(gop), "keyint_min": str(gop)}
        if codec == "h264_nvenc":
            # VBR + constant-quality (cq); no tune=zerolatency (it disables
            # B-frames/lookahead and wrecks quality). Relay tolerates ~1-2s.
            opts.update({"preset": self.nvenc_preset, "rc": "vbr", "cq": self.cq})
        else:
            opts.update({"preset": self.preset, "crf": self.crf})
        if self.maxrate:
            opts["maxrate"] = self.maxrate
            opts["bufsize"] = self.bufsize or self.maxrate
        return opts

    def _stream(self) -> None:
        import av

        LOG.info("stamper %s %s -> %s", self.source_id, self.in_url, self.out_url)
        inp = av.open(self.in_url, options={"rtsp_transport": "tcp"}, timeout=(5.0, 5.0))
        out = None
        try:
            vin = next(s for s in inp.streams if s.type == "video")
            ain = next((s for s in inp.streams if s.type == "audio"), None)

            out = av.open(self.out_url, "w", format="rtsp",
                          options={"rtsp_transport": "tcp"})
            codec = self._video_codec()
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
            # ~2s keyframe interval so the downstream HLS (hls_time 2) and short
            # records are seekable; we control GOP now that we re-encode.
            gop = max(1, int(round(2 * float(vin.average_rate or 15))))
            vout.options = self._video_options(codec, gop)
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
                        for op in vout.encode(frame):
                            out.mux(op)
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
            worker = _StamperWorker(self.relay_host, source_id, self.stop_event)
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
