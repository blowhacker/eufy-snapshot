"""RTSP recording coordinator with thread watchdog and hot-reload."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from .retention import is_live_only
from .video import VideoSegmentDB, VideoWorker

LOG = logging.getLogger(__name__)
_WATCHDOG_INTERVAL = 30


class CaptureWorker:
    def __init__(self, source_db, video_dir: Path, video_db: VideoSegmentDB) -> None:
        self.source_db = source_db
        self.video_dir = video_dir
        self.video_db = video_db
        self.video_workers: dict[str, VideoWorker] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._stop = threading.Event()
        self._watchdog: threading.Thread | None = None

    def start(self) -> None:
        self._sync_sources()
        self._watchdog = threading.Thread(
            target=self._watch, name="rec-watchdog", daemon=True
        )
        self._watchdog.start()

    def _sync_sources(self) -> None:
        if not self.source_db:
            return
        # live_only cameras stay on the realtime wall (raw ingest) but get no
        # recorder: no archive segments, no viewer HLS. A mode flip is picked
        # up here by the 30s watchdog — the removed-source diff below stops
        # the worker of a camera that just went live_only.
        db_sources = {s.id: s for s in self.source_db.to_source_configs()
                      if s.type == "rtsp" and s.enabled
                      and not self._live_only(s.id)}

        for sid in set(self.video_workers) - set(db_sources):
            self.remove_source(sid)

        for sid, source in db_sources.items():
            if sid not in self.video_workers:
                LOG.info("new source, starting: %s", sid)
                vw = VideoWorker(source, self.video_dir, self.video_db)
                self.video_workers[sid] = vw
                self._spawn(sid, vw)

    def _live_only(self, source_id: str) -> bool:
        try:
            return is_live_only(self.source_db, source_id)
        except Exception:
            # Settings must never take recording down — default to recording.
            LOG.warning("record-mode lookup failed for %s", source_id, exc_info=True)
            return False

    def _spawn(self, source_id: str, vw: VideoWorker) -> None:
        t = threading.Thread(target=vw.run, name=f"rec-{source_id}", daemon=True)
        t.start()
        self._threads[source_id] = t
        LOG.info("recording thread started for %s", source_id)

    def _watch(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(_WATCHDOG_INTERVAL)
            if self._stop.is_set():
                break
            self._sync_sources()
            for source_id, vw in list(self.video_workers.items()):
                t = self._threads.get(source_id)
                if t and not t.is_alive():
                    LOG.warning("recording thread dead for %s — restarting", source_id)
                    vw._stop.clear()
                    self._spawn(source_id, vw)

    def thread_health(self) -> dict[str, bool]:
        return {sid: (t.is_alive() if t else False)
                for sid, t in self._threads.items()}

    def recorder_status(self) -> dict[str, dict]:
        workers = list(self.video_workers.items())
        threads = dict(self._threads)
        return {
            sid: {
                "thread_alive": bool(threads.get(sid) and threads[sid].is_alive()),
                **vw.status(),
            }
            for sid, vw in workers
        }

    def remove_source(self, source_id: str) -> None:
        """Stop and join one recorder immediately after its source is removed."""
        worker = self.video_workers.pop(source_id, None)
        thread = self._threads.pop(source_id, None)
        if worker is None:
            return
        LOG.info("source removed, stopping: %s", source_id)
        worker.stop()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=15)

    def stop(self) -> None:
        self._stop.set()
        for vw in self.video_workers.values():
            vw.stop()
        for t in self._threads.values():
            t.join(timeout=15)
