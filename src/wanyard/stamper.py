"""BITC stamper.

Reads a relay path, computes the honest world time per frame (the ONE clock —
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

import numpy as np

from . import bitc
from .live_detector import _list_relay_paths

LOG = logging.getLogger("wanyard.stamper")

_ROLLING_SECONDS = 90.0
_DISC_BACKWARD = -1.0      # rtp step below this (s) = discontinuity → reanchor
_DISC_FORWARD = 5.0        # rtp step above this (s) = discontinuity → reanchor
_PATH_REFRESH_SECONDS = 30.0
_STAMPED_SUFFIX = "-stamped"


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
        self.crf = os.environ.get("WANYARD_STAMP_CRF", "18")
        self.preset = os.environ.get("WANYARD_STAMP_PRESET", "ultrafast")
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
            vout = out.add_stream("libx264", rate=vin.average_rate or 15)
            vout.width = vin.codec_context.width
            vout.height = vin.codec_context.height
            vout.pix_fmt = "yuv420p"
            vout.options = {"preset": self.preset, "tune": "zerolatency",
                            "crf": self.crf}
            aout = None
            if ain is not None:
                try:
                    aout = out.add_stream(template=ain)
                except Exception:
                    LOG.warning("stamper %s audio copy unsupported — video only",
                                self.source_id)
                    aout = None

            anchor = _StampAnchor(self.source_id)
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
                        img = frame.to_ndarray(format="bgr24")
                        try:
                            bitc.render(img, bitc.encode_value(abs_ts))
                        except ValueError:
                            LOG.warning("stamper %s abs_ts %.3f out of range",
                                        self.source_id, abs_ts)
                        of = av.VideoFrame.from_ndarray(img, format="bgr24")
                        of.pts = frame.pts
                        of.time_base = frame.time_base or vin.time_base
                        for op in vout.encode(of):
                            out.mux(op)
                elif aout is not None and packet.stream is ain:
                    packet.stream = aout
                    out.mux(packet)
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
