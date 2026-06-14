from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import logging
import os
import threading
import time
import urllib.request

from . import bitc

LOG = logging.getLogger(__name__)

_QUEUE_LIMIT = 2000
_PATH_REFRESH_SECONDS = 60.0
_MAX_LIVE_LAG_SECONDS = 1.0
_STAMPED_SUFFIX = "-stamped"
_MARKER_FAIL_LOG_SECONDS = 30.0


@dataclass
class _DetectionRow:
    abs_ts: float
    has_human: bool
    confidence: float
    boxes: list[dict]
    classes: list[str]
    segment_id: int | None = None


class _SourceWorker:
    def __init__(self, relay_host: str, source_id: str, relay_path: str, model, video_db,
                 stop_event: threading.Event, predict_lock: threading.Lock,
                 fps: float, claim: bool) -> None:
        self.relay_host = relay_host
        self.source_id = source_id
        self.relay_path = relay_path
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
        self.last_marker_fail_log_wall = 0.0
        self.marker_active_since: float | None = None
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
            try:
                self.marker_active_since = None
                self._stream()
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

    def _stream(self) -> None:
        import av

        url = f"rtsp://{self.relay_host}:8554/{self.relay_path}"
        LOG.info(
            "live detector %s connecting path=%s url=%s",
            self.source_id,
            self.relay_path,
            url,
        )
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
            # Reader/worker split: this thread only demuxes, decodes and parks
            # the freshest frame. BITC decode and YOLO stay in the worker so
            # frames that miss the FPS gate avoid a native-frame ndarray copy.
            latest_lock = threading.Lock()
            latest: list = [None]   # [(frame, wall)] slot
            reader_alive = threading.Event()
            reader_alive.set()

            def _infer_worker() -> None:
                while reader_alive.is_set() and not self.stopped():
                    with latest_lock:
                        item, latest[0] = latest[0], None
                    if item is None:
                        time.sleep(0.02)
                        continue
                    frame, f_wall = item
                    try:
                        self._maybe_infer(frame, f_wall)
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
                    for frame in packet.decode():
                        wall = time.time()
                        with latest_lock:
                            latest[0] = (frame, wall)
                if not self.stopped():
                    raise EOFError("rtsp stream ended")
            finally:
                reader_alive.clear()
                worker.join(timeout=5.0)
        finally:
            container.close()

    def _maybe_infer(self, frame, wall: float) -> None:
        if wall < self.next_infer_wall:
            return
        self.next_infer_wall = wall + (1.0 / self.fps)

        from .video import _CCTV_CLASS_IDS, _CONF_THRESHOLD, _parse_results

        frame_bgr = frame.to_ndarray(format="bgr24")
        abs_ts, crc_ok = bitc.decode(frame_bgr)
        if not crc_ok or abs_ts is None:
            if wall - self.last_marker_fail_log_wall >= _MARKER_FAIL_LOG_SECONDS:
                LOG.info("live detector %s skip frame unreadable BITC marker",
                         self.source_id)
                self.last_marker_fail_log_wall = wall
            return

        if self.marker_active_since is None:
            self.marker_active_since = abs_ts
            LOG.info("live detector %s MARKER_ACTIVE marker_since=%.3f",
                     self.source_id, abs_ts)

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

        bitc.mask(frame_bgr)
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
        )

    def _store_or_queue(self, row: _DetectionRow) -> None:
        segment = self.video_db.open_live_segment(self.source_id)
        self._handle_rotation(segment)
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

    def _handle_rotation(self, segment: dict | None) -> None:
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
        marker_since = self.marker_active_since
        covered = (
            self.claim
            and media_epoch is not None
            and closed.get("end_ts") is not None
            and marker_since is not None
            and marker_since <= float(media_epoch)
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
                "marker_since=%.3f end_ts=%s",
                self.source_id,
                closed["id"],
                float(media_epoch),
                marker_since,
                closed.get("end_ts"),
            )
        else:
            LOG.info(
                "live detector %s left segment_id=%s unclaimed marker_active=%s "
                "media_epoch=%s marker_since=%s",
                self.source_id,
                closed["id"],
                marker_since is not None,
                media_epoch,
                marker_since,
            )


class _LiveDetectorSupervisor:
    def __init__(self, model, video_db, stop_event: threading.Event,
                 predict_lock: threading.Lock) -> None:
        self.model = model
        self.video_db = video_db
        self.stop_event = stop_event
        self.predict_lock = predict_lock
        self.relay_host = os.environ.get("WANYARD_RELAY_HOST", "mediamtx").strip() or "mediamtx"
        self.path_suffix = os.environ.get("WANYARD_RELAY_PATH_SUFFIX", "").strip()
        self.fps = float(os.environ.get("WANYARD_LIVE_FPS", "2.0") or "2.0")
        self.claim = os.environ.get("WANYARD_DETECTOR_CLAIM", "1") == "1"
        self.workers: dict[str, _SourceWorker] = {}
        self.thread = threading.Thread(
            target=self.run,
            daemon=True,
            name="live-detector-supervisor",
        )

    def start(self) -> threading.Thread:
        _configure_torch_threads()
        LOG.info(
            "live detector supervisor starting relay=%s path_suffix=%r fps=%.2f claim=%s",
            self.relay_host,
            self.path_suffix,
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
        targets = _target_relay_paths(_list_relay_paths(self.relay_host), self.path_suffix)
        for source_id in sorted(set(targets) - set(self.workers)):
            worker = _SourceWorker(
                self.relay_host,
                source_id,
                targets[source_id],
                self.model,
                self.video_db,
                self.stop_event,
                self.predict_lock,
                self.fps,
                self.claim,
            )
            self.workers[source_id] = worker
            worker.start()
            LOG.info(
                "live detector started source=%s path=%s",
                source_id,
                targets[source_id],
            )

        for source_id in sorted(set(self.workers) - set(targets)):
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


def _target_relay_paths(paths: list[str], suffix: str) -> dict[str, str]:
    """Map DB source IDs to mediamtx paths the detector should consume.

    With M2 cutover enabled, ``suffix`` is ``-stamped``: the detector reads
    ``<source>-stamped`` but still stores rows under ``<source>``. Without an
    explicit suffix, ignore stamper shadow outputs so the detector does not
    accidentally double-consume M1 paths.
    """
    suffix = suffix.strip()
    targets: dict[str, str] = {}
    for path in sorted({p for p in paths if p}):
        if suffix:
            if not path.endswith(suffix):
                continue
            source_id = path[: -len(suffix)]
            if source_id:
                targets[source_id] = path
        elif not path.endswith(_STAMPED_SUFFIX):
            targets[path] = path
    return targets


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
