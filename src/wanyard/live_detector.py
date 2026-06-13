from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import threading
import time
import urllib.request

LOG = logging.getLogger(__name__)

_ANCHORING_SECONDS = 10.0
_ROLLING_SECONDS = 90.0
_SLEW_SECONDS_PER_SECOND = 0.002
_QUEUE_LIMIT = 2000
_DISC_BACKWARD_SECONDS = -1.0
_DISC_FORWARD_SECONDS = 5.0
_PATH_REFRESH_SECONDS = 60.0
_MAX_LIVE_LAG_SECONDS = 1.0


@dataclass
class _DetectionRow:
    abs_ts: float
    has_human: bool
    confidence: float
    boxes: list[dict]
    classes: list[str]
    segment_id: int | None = None


class _TimeAnchor:
    def __init__(self, source_id: str) -> None:
        self.source_id = source_id
        self.state = "ANCHORING"
        self.anchor_start_wall: float | None = None
        self.anchor_samples: list[tuple[float, float]] = []
        self.rolling: deque[tuple[float, float]] = deque()
        self.delta_active: float | None = None
        self.last_wall: float | None = None
        self.last_rtp: float | None = None
        self.anchored_since: float | None = None
        self.last_slew_log_wall = 0.0
        LOG.info("live detector %s ANCHORING reason=initial", self.source_id)

    @property
    def active(self) -> bool:
        return self.state == "ACTIVE" and self.delta_active is not None

    def _reset(self, reason: str, pts_jump: float | None = None) -> None:
        LOG.info(
            "live detector %s ANCHORING reason=%s pts_jump=%s",
            self.source_id,
            reason,
            f"{pts_jump:.3f}" if pts_jump is not None else "n/a",
        )
        self.state = "ANCHORING"
        self.anchor_start_wall = None
        self.anchor_samples = []
        self.rolling.clear()
        self.delta_active = None
        self.last_wall = None
        self.anchored_since = None

    def observe(self, rtp: float, wall: float) -> float | None:
        if self.last_rtp is not None:
            pts_jump = rtp - self.last_rtp
            if pts_jump < _DISC_BACKWARD_SECONDS or pts_jump > _DISC_FORWARD_SECONDS:
                self.last_rtp = rtp
                self._reset("pts_discontinuity", pts_jump)
                return self._observe_anchoring(rtp, wall)
            if pts_jump < 0:
                LOG.info(
                    "live detector %s skip small pts reorder pts_jump=%.3f",
                    self.source_id,
                    pts_jump,
                )
                self.last_rtp = rtp
                return None
        self.last_rtp = rtp

        if self.state == "ANCHORING":
            return self._observe_anchoring(rtp, wall)
        return self._observe_active(rtp, wall)

    def _observe_anchoring(self, rtp: float, wall: float) -> float | None:
        if self.anchor_start_wall is None:
            self.anchor_start_wall = wall
        delta = wall - rtp
        self.anchor_samples.append((wall, delta))
        if wall - self.anchor_start_wall < _ANCHORING_SECONDS:
            return None

        window_min = min(d for _, d in self.anchor_samples)
        self.delta_active = window_min
        self.rolling = deque(self.anchor_samples)
        self.last_wall = wall
        self.anchored_since = wall
        self.state = "ACTIVE"
        LOG.info(
            "live detector %s ACTIVE delta=%.6f window_min=%.6f samples=%d",
            self.source_id,
            self.delta_active,
            window_min,
            len(self.anchor_samples),
        )
        return None

    def _observe_active(self, rtp: float, wall: float) -> float | None:
        if self.delta_active is None:
            return None
        delta_sample = wall - rtp
        self.rolling.append((wall, delta_sample))
        cutoff = wall - _ROLLING_SECONDS
        while self.rolling and self.rolling[0][0] < cutoff:
            self.rolling.popleft()

        window_min = min(d for _, d in self.rolling)
        elapsed = max(0.0, wall - (self.last_wall or wall))
        max_slew = _SLEW_SECONDS_PER_SECOND * elapsed
        requested = window_min - self.delta_active
        slew = max(-max_slew, min(max_slew, requested))
        self.delta_active += slew
        self.last_wall = wall

        if abs(slew) > 0 and wall - self.last_slew_log_wall >= 30.0:
            LOG.info(
                "live detector %s slew delta=%.6f window_min=%.6f slew=%.6f",
                self.source_id,
                self.delta_active,
                window_min,
                slew,
            )
            self.last_slew_log_wall = wall
        return rtp + self.delta_active


class _SourceWorker:
    def __init__(self, relay_host: str, source_id: str, model, video_db,
                 stop_event: threading.Event, predict_lock: threading.Lock,
                 fps: float, claim: bool) -> None:
        self.relay_host = relay_host
        self.source_id = source_id
        self.model = model
        self.video_db = video_db
        self.stop_event = stop_event
        self.predict_lock = predict_lock
        self.fps = max(0.1, float(fps))
        self.claim = claim
        self.local_stop = threading.Event()
        self.pending: deque[_DetectionRow] = deque()
        self.open_segment_id: int | None = None
        self.next_infer_wall = 0.0
        self.last_stale_log_wall = 0.0
        # Honest per-frame world times, shared with the recorder for segment
        # anchoring (see video.py _align_media_epoch). Ring of two files.
        video_dir = Path(os.environ.get("VIDEO_DIR", "video"))
        self._ft_path = video_dir / "live" / source_id / "frametimes.jsonl"
        self._ft_buf: list[tuple[float, float]] = []
        self.thread = threading.Thread(
            target=self.run,
            daemon=True,
            name=f"live-detector-{source_id}",
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.local_stop.set()

    def stopped(self) -> bool:
        return self.stop_event.is_set() or self.local_stop.is_set()

    def run(self) -> None:
        backoff = 1.0
        while not self.stopped():
            anchor = _TimeAnchor(self.source_id)
            try:
                self._stream(anchor)
                backoff = 1.0
            except Exception as exc:
                LOG.warning(
                    "live detector %s reconnect in %.1fs after %s",
                    self.source_id,
                    backoff,
                    exc,
                )
                self._wait(backoff)
                backoff = min(30.0, backoff * 2.0)
        LOG.info("live detector %s stopped", self.source_id)

    def _wait(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while not self.stopped() and time.time() < deadline:
            time.sleep(min(0.25, deadline - time.time()))

    def _stream(self, anchor: _TimeAnchor) -> None:
        import av

        url = f"rtsp://{self.relay_host}:8554/{self.source_id}"
        LOG.info("live detector %s connecting %s", self.source_id, url)
        container = av.open(
            url,
            options={"rtsp_transport": "tcp"},
            timeout=(5.0, 5.0),
        )
        try:
            video_streams = [s for s in container.streams if s.type == "video"]
            if not video_streams:
                raise RuntimeError("no video stream")
            stream = video_streams[0]
            LOG.info(
                "live detector %s connected time_base=%s",
                self.source_id,
                stream.time_base,
            )
            # Reader/worker split: this thread only demuxes, timestamps,
            # decodes and parks the freshest frame — it must NEVER block on
            # inference, or packets queue in the socket and the recorded
            # arrival walls become a burst pattern of the drain instead of
            # delivery (which breaks the recorder's jitter-pattern anchor
            # alignment AND batches stale frames into YOLO).
            latest_lock = threading.Lock()
            latest: list = [None]   # [(frame, abs_ts, wall)] slot
            reader_alive = threading.Event()
            reader_alive.set()

            def _infer_worker() -> None:
                while reader_alive.is_set() and not self.stopped():
                    with latest_lock:
                        item, latest[0] = latest[0], None
                    if item is None:
                        time.sleep(0.02)
                        continue
                    frame, f_abs, f_wall = item
                    try:
                        self._maybe_infer(frame, f_abs, f_wall, anchor)
                    except Exception:
                        LOG.exception("live detector %s inference error",
                                      self.source_id)

            worker = threading.Thread(
                target=_infer_worker, daemon=True,
                name=f"live-infer-{self.source_id}")
            worker.start()
            try:
                for packet in container.demux(stream):
                    if self.stopped():
                        break
                    if packet.pts is None:
                        continue
                    wall = time.time()
                    rtp = float(packet.pts * stream.time_base)
                    for frame in packet.decode():
                        abs_ts = anchor.observe(rtp, wall)
                        if abs_ts is None:
                            continue
                        self._dump_frame_time(abs_ts, wall)
                        with latest_lock:
                            latest[0] = (frame, abs_ts, wall)
                if not self.stopped():
                    raise EOFError("rtsp stream ended")
            finally:
                reader_alive.clear()
                worker.join(timeout=5.0)
        finally:
            container.close()

    def _dump_frame_time(self, abs_ts: float, wall: float) -> None:
        """Append per-frame (honest world time, arrival wall) to the ring file.

        The recorder aligns its MP4 pts sequence against the WALL column at
        segment close — relay delivery jitter is common-mode across consumers
        (B0 re-measured: cross-consumer per-frame delay diff p99 = 0.2 ms), so
        the jitter pattern pairs frames unambiguously even on perfect-CFR
        cameras. media_epoch then comes from the ABS column (detector clock).
        Open/append/close per flush — no held fds (gotcha #7 discipline).
        """
        self._ft_buf.append((abs_ts, wall))
        if len(self._ft_buf) < 100:
            return
        buf, self._ft_buf = self._ft_buf, []
        try:
            self._ft_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._ft_path, "a", encoding="utf-8") as f:
                f.write("".join(f"{a:.6f} {w:.6f}\n" for a, w in buf))
            if self._ft_path.stat().st_size > 6_000_000:   # ~2h per generation
                os.replace(self._ft_path, self._ft_path.with_suffix(".jsonl.1"))
        except OSError:
            LOG.exception("live detector %s frametimes dump failed", self.source_id)

    def _maybe_infer(self, frame, abs_ts: float, wall: float,
                     anchor: _TimeAnchor) -> None:
        lag = wall - abs_ts
        if lag > _MAX_LIVE_LAG_SECONDS:
            if wall - self.last_stale_log_wall >= 30.0:
                LOG.info(
                    "live detector %s dropping stale decoded frames lag=%.3f",
                    self.source_id,
                    lag,
                )
                self.last_stale_log_wall = wall
            return
        if wall < self.next_infer_wall:
            return
        self.next_infer_wall = wall + (1.0 / self.fps)

        from .video import _CCTV_CLASS_IDS, _CONF_THRESHOLD, _parse_results

        frame_bgr = frame.to_ndarray(format="bgr24")
        with self.predict_lock:
            results = self.model.predict(
                frame_bgr,
                imgsz=640,
                classes=_CCTV_CLASS_IDS,
                conf=_CONF_THRESHOLD,
                verbose=False,
            )
        has_human, confidence, boxes = _parse_results(results)
        classes = list({b["cls"] for b in boxes}) if boxes else []
        self._store_or_queue(
            _DetectionRow(abs_ts, has_human, confidence, boxes, classes),
            anchor,
        )

    def _store_or_queue(self, row: _DetectionRow, anchor: _TimeAnchor) -> None:
        segment = self.video_db.open_live_segment(self.source_id)
        self._handle_rotation(segment, anchor)
        if not segment or segment.get("media_epoch") is None:
            row.segment_id = int(segment["id"]) if segment else None
            self._queue(row)
            return

        self._flush_pending(segment)
        self.video_db.insert_live_detections(
            int(segment["id"]),
            self.source_id,
            [row.__dict__],
        )

    def _queue(self, row: _DetectionRow) -> None:
        if len(self.pending) >= _QUEUE_LIMIT:
            self.pending.popleft()
            LOG.warning(
                "live detector %s pending queue full; dropped oldest row",
                self.source_id,
            )
        self.pending.append(row)

    def _flush_pending(self, segment: dict) -> None:
        if not self.pending:
            return
        segment_id = int(segment["id"])
        rows: list[_DetectionRow] = []
        dropped = 0
        while self.pending:
            row = self.pending.popleft()
            if row.segment_id is not None and row.segment_id != segment_id:
                dropped += 1
                continue
            rows.append(row)
        if rows:
            try:
                self.video_db.insert_live_detections(
                    segment_id,
                    self.source_id,
                    [r.__dict__ for r in rows],
                )
            except Exception:
                for queued in reversed(rows):
                    self.pending.appendleft(queued)
                raise
            LOG.info(
                "live detector %s flushed queued rows=%d segment_id=%d",
                self.source_id,
                len(rows),
                segment_id,
            )
        if dropped:
            LOG.warning(
                "live detector %s dropped stale queued rows=%d",
                self.source_id,
                dropped,
            )

    def _handle_rotation(self, segment: dict | None, anchor: _TimeAnchor) -> None:
        new_id = int(segment["id"]) if segment else None
        if self.open_segment_id is None:
            self.open_segment_id = new_id
            return
        if new_id == self.open_segment_id:
            return

        closed_id = self.open_segment_id
        self.open_segment_id = new_id
        closed = self.video_db.get_segment(closed_id)
        if not closed:
            return

        media_epoch = closed.get("media_epoch")
        anchored_since = anchor.anchored_since
        covered = (
            self.claim
            and anchor.active
            and media_epoch is not None
            and closed.get("end_ts") is not None
            and anchored_since is not None
            and anchored_since <= float(media_epoch)
        )
        if covered:
            self.video_db.mark_scanned(int(closed["id"]))
            try:
                from .video import extract_events

                dets = self.video_db.detections_for_segment(int(closed["id"]))
                n_evt = extract_events(closed, dets, self.video_db)
                LOG.info(
                    "live detector %s extracted %d events for claimed segment_id=%s",
                    self.source_id,
                    n_evt,
                    closed["id"],
                )
            except Exception:
                LOG.exception(
                    "live detector %s failed to derive events for claimed segment_id=%s",
                    self.source_id,
                    closed["id"],
                )
            LOG.info(
                "live detector %s claimed segment_id=%s media_epoch=%.3f "
                "anchored_since=%.3f end_ts=%s",
                self.source_id,
                closed["id"],
                float(media_epoch),
                anchored_since,
                closed.get("end_ts"),
            )
        else:
            LOG.info(
                "live detector %s left segment_id=%s unclaimed active=%s "
                "media_epoch=%s anchored_since=%s",
                self.source_id,
                closed["id"],
                anchor.active,
                media_epoch,
                anchored_since,
            )


class _LiveDetectorSupervisor:
    def __init__(self, model, video_db, stop_event: threading.Event,
                 predict_lock: threading.Lock) -> None:
        self.model = model
        self.video_db = video_db
        self.stop_event = stop_event
        self.predict_lock = predict_lock
        self.relay_host = os.environ.get("WANYARD_RELAY_HOST", "mediamtx").strip() or "mediamtx"
        self.fps = float(os.environ.get("WANYARD_LIVE_FPS", "2.0") or "2.0")
        self.claim = os.environ.get("WANYARD_SHADOW_CLAIM", "1") == "1"
        self.workers: dict[str, _SourceWorker] = {}
        self.thread = threading.Thread(
            target=self.run,
            daemon=True,
            name="live-detector-supervisor",
        )

    def start(self) -> threading.Thread:
        _configure_torch_threads()
        LOG.info(
            "live detector supervisor starting relay=%s fps=%.2f claim=%s",
            self.relay_host,
            self.fps,
            self.claim,
        )
        self.thread.start()
        return self.thread

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._sync_paths()
            except Exception:
                LOG.exception("live detector path discovery failed")
            self.stop_event.wait(_PATH_REFRESH_SECONDS)
        for worker in self.workers.values():
            worker.stop()
        LOG.info("live detector supervisor stopped")

    def _sync_paths(self) -> None:
        paths = set(_list_relay_paths(self.relay_host))
        for source_id in sorted(paths - set(self.workers)):
            worker = _SourceWorker(
                self.relay_host,
                source_id,
                self.model,
                self.video_db,
                self.stop_event,
                self.predict_lock,
                self.fps,
                self.claim,
            )
            self.workers[source_id] = worker
            worker.start()
            LOG.info("live detector started path=%s", source_id)

        for source_id in sorted(set(self.workers) - paths):
            self.workers[source_id].stop()
            del self.workers[source_id]
            LOG.info("live detector stopped removed path=%s", source_id)


def _list_relay_paths(relay_host: str) -> list[str]:
    url = f"http://{relay_host}:9997/v3/paths/list"
    with urllib.request.urlopen(url, timeout=5) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    paths = []
    for item in payload.get("items") or []:
        name = item.get("name")
        if name:
            paths.append(str(name))
    return paths


def _configure_torch_threads() -> None:
    raw = os.environ.get("OMP_NUM_THREADS", "").strip()
    if not raw:
        return
    try:
        threads = int(raw)
    except ValueError:
        LOG.warning("invalid OMP_NUM_THREADS=%r", raw)
        return
    if threads <= 0:
        return
    try:
        import torch
        torch.set_num_threads(threads)
        try:
            torch.set_num_interop_threads(max(1, threads))
        except RuntimeError as exc:
            LOG.info("torch interop threads already set: %s", exc)
        LOG.info("live detector set torch threads=%d", threads)
    except Exception:
        LOG.exception("failed to set torch threads")
    try:
        import cv2
        cv2.setNumThreads(0)
        LOG.info("live detector disabled OpenCV internal threads")
    except Exception:
        LOG.exception("failed to configure OpenCV threads")


def start_live_detector(model, video_db, stop_event: threading.Event,
                        predict_lock: threading.Lock) -> threading.Thread:
    return _LiveDetectorSupervisor(
        model,
        video_db,
        stop_event,
        predict_lock,
    ).start()
