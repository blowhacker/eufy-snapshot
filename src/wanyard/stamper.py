"""Frame-clock stamper.

Reads a relay path, computes the Unix time per frame (the ONE clock —
rolling-min of wall-minus-rtp), and attaches it to every video frame as an
H.264 SEI NAL, republishing to ``<src>-stamped``. Default ``sei_copy`` injects
the SEI packet-level with no re-encode (zero generation loss); ``reencode`` is
the non-H.264 fallback and emits the same SEI on its encoded packets. Audio is
remuxed (copied) through. Downstream consumers (recorder, detector) read the
time from the frame's SEI instead of running their own clock — no
cross-consumer reconciliation.

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
from pathlib import Path

import numpy as np

from . import sei
from .live_detector import _list_relay_paths
from .media_health import MediaHealthStore, default_db_path

LOG = logging.getLogger("wanyard.stamper")

_ROLLING_SECONDS = 90.0
_DISC_BACKWARD = -1.0      # rtp step below this (s) = discontinuity → reanchor
_DISC_FORWARD = 5.0        # rtp step above this (s) = discontinuity → reanchor
_PATH_REFRESH_SECONDS = 30.0
_SUPERVISOR_POLL_SECONDS = 1.0
_STAMPED_SUFFIX = "-stamped"

_nvenc_probe: dict[str, bool] = {}


class _InputMetadataNotReady(RuntimeError):
    """The RTSP stream opened before codec dimensions were available."""


class _ClockEpochDiscontinuity(RuntimeError):
    """Trusted clock advanced too far to remain in one media timeline."""

    def __init__(self, gap_seconds: float) -> None:
        self.gap_seconds = float(gap_seconds)
        super().__init__(f"trusted clock gap of {self.gap_seconds:.3f}s")


class _MediaTimeline:
    """Map authoritative clock time onto one monotonic media epoch.

    The clock is authoritative. Camera RTP is only an input to the estimator and
    must not independently shape playback time. Non-increasing clock samples are
    stale and are discarded. A real gap closes this epoch so downstream media
    represents missing camera coverage as a gap between assets.
    """

    def __init__(self, *, max_gap_seconds: float, ticks_per_second: int = 90000) -> None:
        self.max_gap_seconds = float(max_gap_seconds)
        self.ticks_per_second = int(ticks_per_second)
        self.epoch: float | None = None
        self.last_clock: float | None = None
        self.last_pts = -1

    def pts(self, clock_ts: float, *, nudge: bool = False) -> int | None:
        """Media pts for ``clock_ts``; None for unusable (stale) samples.

        ``nudge=True`` is for codec-copy mode, where a packet can never be
        dropped (dropping a reference frame corrupts the GOP downstream): a
        non-advancing clock sample yields ``last_pts + 1`` — one 90kHz tick,
        11µs of timeline — instead of None. Real gaps still raise
        :class:`_ClockEpochDiscontinuity` in both modes.
        """
        if not np.isfinite(clock_ts):
            return None
        if self.epoch is None:
            self.epoch = clock_ts
            self.last_clock = clock_ts
            self.last_pts = 0
            return 0

        assert self.last_clock is not None
        step = clock_ts - self.last_clock
        if step <= 0:
            if not nudge:
                return None
            # Clock did not advance: keep last_clock (the estimator's truth),
            # emit the next representable pts so the container stays monotonic.
            self.last_pts += 1
            return self.last_pts
        if step > self.max_gap_seconds:
            raise _ClockEpochDiscontinuity(step)

        pts = int(round((clock_ts - self.epoch) * self.ticks_per_second))
        if pts <= self.last_pts:
            if not nudge:
                return None
            pts = self.last_pts + 1
        self.last_clock = clock_ts
        self.last_pts = pts
        return pts


def _nvenc_available(codec: str = "h264_nvenc") -> bool:
    """True if the H.264 NVENC codec can actually open and encode here.

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
                 health_store: MediaHealthStore | None = None,
                 source_db_path: str | os.PathLike | None = None) -> None:
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
        # The SEI clock is a NAL, so it is bitrate-independent regardless of cap.
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
        self._invalid_metadata_count = 0
        self._last_invalid_metadata_event_ts = 0.0
        self._source_db_path = source_db_path
        try:
            self.epoch_gap_seconds = float(
                self._src_env("EPOCH_GAP_SECONDS", "2.0") or "2.0"
            )
        except ValueError:
            self.epoch_gap_seconds = 2.0
        if not np.isfinite(self.epoch_gap_seconds) or self.epoch_gap_seconds <= 0:
            self.epoch_gap_seconds = 2.0
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
            except _ClockEpochDiscontinuity as exc:
                self._health_state("restarting_after_clock_gap", last_error=str(exc))
                self._health_event(
                    "warning",
                    "clock_gap",
                    (
                        f"trusted clock gap of {exc.gap_seconds:.3f}s; "
                        "starting a new recording epoch"
                    ),
                    {"gap_seconds": exc.gap_seconds},
                )
                LOG.warning(
                    "stamper %s trusted clock gap %.3fs; starting new media epoch",
                    self.source_id,
                    exc.gap_seconds,
                )
                self._wait(0.25)
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
        runtime), fall back h264_nvenc → libx264 for the rest of the process so
        the camera stays alive (CPU) instead of looping on a codec that can't
        open. Non-open errors (network etc.) take the normal reconnect."""
        demote = {"h264_nvenc": "libx264"}
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
        """Resolve the re-encode fallback's output encoder — H.264 only.

        The clock rides as an H.264 SEI NAL on the encoded packets, so the
        fallback emits h264: h264_nvenc when the GPU probe succeeds, else
        libx264 (a GPU-less box — the default `docker compose up` — lands on
        libx264, no crash loop).
        """
        if self._codec_override:
            return self._codec_override   # runtime fallback after an open failure
        if self.encoder == "libx264":
            return "libx264"
        requested = self.encoder or "auto"
        if requested not in ("auto", "nvenc", "h264_nvenc"):
            LOG.warning("stamper %s encoder=%s unsupported (h264 only) — "
                        "using auto", self.source_id, requested)
        if requested in ("nvenc", "h264_nvenc"):
            return "h264_nvenc"
        if _nvenc_available("h264_nvenc"):
            LOG.info("stamper %s encoder=auto resolved to h264_nvenc",
                     self.source_id)
            return "h264_nvenc"
        LOG.info("stamper %s encoder=auto resolved to libx264 (no usable NVENC)",
                 self.source_id)
        return "libx264"

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
                         "cq": self.cq,
                         "bf": "0", "rc-lookahead": "0",
                         "delay": "0"})
        else:
            # zerolatency: same reason — keep the re-encode within the live
            # detector's 1.0s staleness gate.
            opts.update({"preset": self.preset, "tune": "zerolatency",
                         "crf": self.crf})
        maxrate = self._src_env("H264_MAXRATE", self.maxrate)
        bufsize = self._src_env("H264_BUFSIZE", self.bufsize)
        if maxrate:
            opts["maxrate"] = maxrate
            opts["bufsize"] = bufsize or maxrate
        return opts

    def _resolve_stamp_mode(self) -> str:
        """Per-source stamp mode: DB setting > WANYARD_STAMP_MODE env > sei_copy.

        sei_copy is the default everywhere; reencode survives as the
        automatic fallback for non-h264 input and as an explicit per-source
        escape hatch (it re-encodes and re-attaches the same SEI clock).
        """
        mode = None
        if self._source_db_path is not None:
            try:
                from .db import SourceDB

                mode = sei.stamp_mode(
                    SourceDB(Path(self._source_db_path)), self.source_id,
                    default=None,
                )
            except Exception:
                LOG.warning("stamper %s could not read stamp mode settings",
                            self.source_id, exc_info=True)
        if mode is None:
            mode = sei.normalize_stamp_mode(
                self._src_env("MODE"), sei.STAMP_MODE_SEI_COPY
            )
        return mode

    def _add_audio_stream(self, out, ain):
        """Copy-through audio stream on ``out``; None when unsupported."""
        aout = None
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
        return aout

    def _video_metadata(self, vin) -> tuple[int, int, float]:
        width = int(getattr(vin.codec_context, "width", 0) or 0)
        height = int(getattr(vin.codec_context, "height", 0) or 0)
        if width <= 0 or height <= 0:
            self._invalid_metadata_count += 1
            message = f"input metadata not ready: {width}x{height}"
            self._health_state(
                "waiting_for_input_metadata",
                width=width,
                height=height,
                last_error=message,
            )
            now = time.time()
            if now - self._last_invalid_metadata_event_ts >= 300:
                self._health_event(
                    "warning",
                    "input_metadata_invalid",
                    message,
                    {
                        "width": width,
                        "height": height,
                        "retry_count": self._invalid_metadata_count,
                    },
                )
                self._last_invalid_metadata_event_ts = now
            raise _InputMetadataNotReady(message)

        try:
            fps = float(vin.average_rate or 15)
        except (TypeError, ValueError, ZeroDivisionError):
            fps = 15.0
        if not np.isfinite(fps) or fps <= 0:
            fps = 15.0
        return width, height, fps

    def _stream(self) -> None:
        import av

        LOG.info("stamper %s %s -> %s", self.source_id, self.in_url, self.out_url)
        self._health_state("connecting")
        inp = av.open(self.in_url, options={"rtsp_transport": "tcp"}, timeout=(5.0, 5.0))
        out = None
        try:
            vin = next(s for s in inp.streams if s.type == "video")
            ain = next((s for s in inp.streams if s.type == "audio"), None)
            width, height, fps = self._video_metadata(vin)

            mode = self._resolve_stamp_mode()
            if mode == sei.STAMP_MODE_SEI_COPY:
                input_codec = getattr(vin.codec_context, "name", "")
                if input_codec == "h264":
                    self._stream_sei_copy(inp, vin, ain, width, height, fps)
                    return
                LOG.warning(
                    "stamper %s stamp_mode=sei_copy needs h264 input, got %s — "
                    "falling back to re-encode", self.source_id, input_codec,
                )

            out = av.open(self.out_url, "w", format="rtsp",
                          options={"rtsp_transport": "tcp"})
            codec = self._video_codec()
            self._active_codec = codec
            vout = out.add_stream(codec, rate=vin.average_rate or 15)
            vout.width = width
            vout.height = height
            vout.pix_fmt = "yuv420p"
            # Fixed 90kHz output timebase. PTS is derived below from the exact
            # clock value carried on each frame's SEI, so stamp time and
            # playback time have one authoritative clock.
            out_tb = Fraction(1, 90000)
            vout.codec_context.time_base = out_tb
            # Keyframe interval. HLS segments can only break on keyframes, so the
            # GOP is the floor on live segment duration (and thus live latency).
            # Default 2s (seekable records); set WANYARD_STAMP_GOP_SECONDS=1 for
            # lower-latency live (more I-frames, slightly fatter).
            gop_seconds = float(self._src_env("GOP_SECONDS", "2"))
            gop = max(1, int(round(gop_seconds * fps)))
            video_options = self._video_options(codec, gop)
            vout.options = video_options
            self._health_state(
                "connecting",
                active_encoder=codec,
                width=width,
                height=height,
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
                        "width": width,
                        "height": height,
                        "fps": fps,
                        "maxrate": video_options.get("maxrate"),
                        "bufsize": video_options.get("bufsize"),
                    },
                )
                self._reported_encoder = codec
            aout = self._add_audio_stream(out, ain) if ain is not None else None

            anchor = _StampAnchor(self.source_id)
            timeline = _MediaTimeline(max_gap_seconds=self.epoch_gap_seconds)
            # Clock values for frames submitted to the encoder, keyed by the
            # pts we assigned. The encoder may hold frames briefly (startup
            # delay), so the value is re-attached to the packet that carries
            # that exact pts — same per-frame binding as the copy path.
            pending_values: dict[int, int] = {}
            last_health_update = 0.0

            def _mux_encoded(packets) -> None:
                # Defensive bound: with codec tb == 1/90000 every packet pts
                # matches a submitted frame exactly, but never let a mismatch
                # grow the map unbounded.
                while len(pending_values) > 300:
                    pending_values.pop(next(iter(pending_values)))
                for op in packets:
                    value = pending_values.pop(op.pts, None)
                    if value is not None:
                        injected = sei.inject(bytes(op), value)
                        if injected is not None:
                            np_ = av.Packet(injected)
                            np_.pts = op.pts
                            np_.dts = op.dts
                            np_.time_base = op.time_base
                            np_.stream = op.stream
                            np_.is_keyframe = op.is_keyframe
                            op = np_
                    out.mux(op)

            for packet in inp.demux():
                if self.stopped():
                    break
                if packet.dts is None:
                    continue
                if packet.stream is vin:
                    for frame in packet.decode():
                        wall = time.time()
                        frame_pts = frame.pts if frame.pts is not None else packet.pts
                        frame_tb = frame.time_base or vin.time_base
                        rtp = (
                            float(frame_pts * frame_tb)
                            if frame_pts is not None and frame_tb is not None
                            else wall
                        )
                        abs_ts = anchor.observe(rtp, wall)
                        pts = timeline.pts(abs_ts)
                        if pts is None:
                            LOG.debug(
                                "stamper %s dropped stale frame %.6f",
                                self.source_id,
                                abs_ts,
                            )
                            continue
                        # The clock rides as an SEI NAL on the encoded packet
                        # (injected in _mux_encoded) — the re-encode fallback
                        # emits the same SEI carrier as sei_copy.
                        try:
                            pending_values[pts] = sei.encode_value(abs_ts)
                        except ValueError:
                            LOG.warning("stamper %s abs_ts %.3f out of range",
                                        self.source_id, abs_ts)
                        if frame.format.name != "yuv420p":
                            frame = frame.reformat(format="yuv420p")
                        frame.pts = pts
                        frame.time_base = out_tb
                        _mux_encoded(vout.encode(frame))
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
                _mux_encoded(vout.encode())
                raise EOFError("rtsp input ended")
        finally:
            if out is not None:
                try:
                    out.close()
                except Exception:
                    pass
            inp.close()


    def _stream_sei_copy(self, inp, vin, ain, width, height, fps) -> None:
        """Codec-copy stamping: per-AU SEI injection, no decode, no encode.

        The camera bitstream passes through bit-identical (zero generation
        loss, archive size = camera native, no NVENC session); each video
        packet gains an unregistered SEI NAL carrying the clock's centisecond
        value + CRC, and its pts is rewritten on the same clock media timeline.
        Packets are never
        dropped mid-GOP (that would corrupt downstream decode): the timeline
        runs in nudge mode, and startup waits for a keyframe so the copy
        starts on a clean GOP. ``inp`` is closed by the caller's finally.
        """
        import av

        out = av.open(self.out_url, "w", format="rtsp",
                      options={"rtsp_transport": "tcp"})
        try:
            vout = out.add_stream_from_template(vin)
            out_tb = Fraction(1, 90000)
            self._active_codec = "copy"
            self._health_state("connecting", active_encoder="copy",
                               width=width, height=height, fps=fps)
            if "copy" != self._reported_encoder:
                self._health_event(
                    "info", "encoder_selected",
                    "stamp mode sei_copy: h264 passthrough + SEI timecode",
                    {"encoder": "copy", "width": width, "height": height,
                     "fps": fps},
                )
                self._reported_encoder = "copy"
            aout = self._add_audio_stream(out, ain) if ain is not None else None

            anchor = _StampAnchor(self.source_id)
            timeline = _MediaTimeline(max_gap_seconds=self.epoch_gap_seconds)
            wait_key = True
            inject_fail_logged = False
            last_health_update = 0.0
            for packet in inp.demux():
                if self.stopped():
                    break
                if packet.dts is None:
                    continue
                if packet.stream is vin:
                    wall = time.time()
                    if wait_key:
                        if not packet.is_keyframe:
                            continue
                        wait_key = False
                    tb = packet.time_base or vin.time_base
                    base_pts = packet.pts if packet.pts is not None else packet.dts
                    rtp = (
                        float(base_pts * tb)
                        if base_pts is not None and tb is not None
                        else wall
                    )
                    abs_ts = anchor.observe(rtp, wall)
                    pts = timeline.pts(abs_ts, nudge=True)
                    if pts is None:
                        # Only a non-finite abs_ts lands here; nothing sane to
                        # stamp or time it with.
                        continue
                    data = bytes(packet)
                    try:
                        value = sei.encode_value(abs_ts)
                    except ValueError:
                        LOG.warning("stamper %s abs_ts %.3f out of range",
                                    self.source_id, abs_ts)
                    else:
                        injected = sei.inject(data, value)
                        if injected is not None:
                            data = injected
                        elif not inject_fail_logged:
                            LOG.warning(
                                "stamper %s could not inject SEI (no slice NAL "
                                "found) — passing packets through untimed",
                                self.source_id,
                            )
                            inject_fail_logged = True
                    op = av.Packet(data)
                    op.pts = pts
                    op.dts = pts
                    op.time_base = out_tb
                    op.stream = vout
                    op.is_keyframe = packet.is_keyframe
                    out.mux(op)
                    if wall - last_health_update >= 15.0:
                        self._health_state("live", active_encoder="copy",
                                           width=width, height=height, fps=fps)
                        if self._reconnect_count > self._reported_recovery_count:
                            self._health_event(
                                "info", "stream_recovered",
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
                raise EOFError("rtsp input ended")
        finally:
            try:
                out.close()
            except Exception:
                pass


class _StamperSupervisor:
    def __init__(
        self,
        stop_event: threading.Event,
        *,
        source_db_path: str | os.PathLike | None = None,
    ) -> None:
        self.stop_event = stop_event
        self.relay_host = os.environ.get("WANYARD_RELAY_HOST", "mediamtx").strip() or "mediamtx"
        self.workers: dict[str, _StamperWorker] = {}
        self.source_db_path = source_db_path
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
        next_path_refresh = 0.0
        while not self.stop_event.is_set():
            now = time.time()
            if now >= next_path_refresh:
                try:
                    self._sync_paths()
                except Exception:
                    LOG.exception("stamper path discovery failed")
                next_path_refresh = now + _PATH_REFRESH_SECONDS
            self.stop_event.wait(_SUPERVISOR_POLL_SECONDS)
        for worker in self.workers.values():
            worker.stop()
        LOG.info("stamper supervisor stopped")

    def _make_worker(self, source_id: str) -> _StamperWorker:
        return _StamperWorker(
            self.relay_host,
            source_id,
            self.stop_event,
            health_store=self.health_store,
            source_db_path=self.source_db_path,
        )

    def _live_only(self, source_id: str) -> bool:
        """True when the camera is view-only (realtime wall, no pipeline).

        Skipping the stamp saves an NVENC session + CPU, and because the live
        detector only follows ``-stamped`` relay paths it also disables
        detection/notifications for the camera — intended: live_only cameras
        produce no footage for notifications to point at. Any settings error
        defaults to stamping (never silently drop a recorded camera).
        """
        if self.source_db_path is None:
            return False
        try:
            from .db import SourceDB
            from .retention import is_live_only

            return is_live_only(SourceDB(Path(self.source_db_path)), source_id)
        except Exception:
            LOG.warning("record-mode lookup failed for %s", source_id, exc_info=True)
            return False

    def _sync_paths(self) -> None:
        # Source paths only — never stamp our own output (would loop) — and
        # only cameras that record: live_only paths get no stamper worker, so
        # no -stamped output, no recorder input, no detection. A mode flip is
        # picked up by the periodic path refresh; the removed-path diff below
        # stops the worker of a camera that just went live_only.
        paths = {p for p in _list_relay_paths(self.relay_host)
                 if not p.endswith(_STAMPED_SUFFIX) and not self._live_only(p)}
        for source_id in sorted(paths - set(self.workers)):
            worker = self._make_worker(source_id)
            self.workers[source_id] = worker
            worker.start()
            LOG.info("stamper started path=%s", source_id)
        for source_id in sorted(set(self.workers) - paths):
            self.workers[source_id].stop()
            del self.workers[source_id]
            LOG.info("stamper stopped removed path=%s", source_id)


def run(config=None) -> int:
    import signal

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    stop_event = threading.Event()

    def _shutdown(sig, frame):
        LOG.info("stamper shutting down")
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    source_db_path = getattr(config, "db_path", None)
    sup = _StamperSupervisor(stop_event, source_db_path=source_db_path)
    sup.start()
    while not stop_event.is_set():
        time.sleep(1.0)
    sup.thread.join(timeout=10.0)
    return 0
