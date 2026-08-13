from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard import video
from wanyard.config import SourceConfig
from wanyard.video import VideoSegmentDB, VideoWorker


RUN_FAULT_TESTS = os.environ.get("WANYARD_RUN_RECORDER_FAULT_TESTS") == "1"
RTSP_HOST = os.environ.get("WANYARD_FAULT_RTSP_HOST", "mediamtx")
RTSP_PORT = 8554


def _wait_for(description: str, predicate, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:
            last_error = exc
        time.sleep(0.1)
    detail = f": {last_error}" if last_error else ""
    raise AssertionError(f"timed out waiting for {description}{detail}")


def _wait_for_port() -> None:
    def ready() -> bool:
        with socket.create_connection((RTSP_HOST, RTSP_PORT), timeout=0.5):
            return True

    _wait_for("MediaMTX RTSP port", ready)


def _probe_codec(url: str) -> str | None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-rtsp_transport",
                "tcp",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=nw=1:nk=1",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (result.stdout or "").strip().lower() or None


class _Publisher:
    def __init__(self, path: str, codec: str) -> None:
        self.path = path
        self.codec = codec
        self.url = f"rtsp://{RTSP_HOST}:{RTSP_PORT}/{path}"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        if self.codec == "hevc":
            encoder = [
                "-c:v",
                "libx265",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-x265-params",
                "log-level=error:keyint=10:min-keyint=10:scenecut=0",
            ]
        elif self.codec == "h264":
            encoder = [
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-g",
                "10",
                "-keyint_min",
                "10",
                "-sc_threshold",
                "0",
            ]
        else:
            raise ValueError(self.codec)

        self.proc = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=10",
                *encoder,
                "-pix_fmt",
                "yuv420p",
                "-an",
                "-f",
                "rtsp",
                "-rtsp_transport",
                "tcp",
                self.url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        def published() -> bool:
            if self.proc and self.proc.poll() is not None:
                error = (self.proc.stderr.read() or b"").decode("utf-8", "replace")
                raise AssertionError(
                    f"{self.codec} publisher exited with {self.proc.returncode}: {error}"
                )
            return _probe_codec(self.url) == self.codec

        try:
            _wait_for(f"{self.codec} publisher on {self.path}", published)
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        proc = self.proc
        self.proc = None
        if not proc:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if proc.stderr:
            proc.stderr.close()


class _RetryRecordingEvent:
    def __init__(self, retry_threshold: float, stop_after: int) -> None:
        self._event = threading.Event()
        self.retry_threshold = retry_threshold
        self.stop_after = stop_after
        self.retry_delays: list[float] = []

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()

    def clear(self) -> None:
        self._event.clear()

    def wait(self, timeout: float | None = None) -> bool:
        delay = float(timeout or 0.0)
        if delay >= self.retry_threshold:
            self.retry_delays.append(delay)
            if len(self.retry_delays) >= self.stop_after:
                self._event.set()
            return self._event.wait(0.01)
        return self._event.wait(min(delay, 0.02))


@unittest.skipUnless(
    RUN_FAULT_TESTS,
    "run with ./scripts/test-recorder-faults.sh",
)
class RecorderFaultIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _wait_for_port()
        encoders = subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-encoders"],
            text=True,
            stderr=subprocess.STDOUT,
        )
        for encoder in ("libx264", "libx265"):
            if encoder not in encoders:
                raise unittest.SkipTest(f"FFmpeg encoder {encoder} is unavailable")

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(video, "_MAX_SEGMENT_SECONDS", 2.0))
        self.stack.enter_context(mock.patch.object(video, "_RECORD_POLL_SECONDS", 0.1))
        self.stack.enter_context(
            mock.patch.object(video, "_RECORD_RETRY_INITIAL_SECONDS", 0.1)
        )
        self.stack.enter_context(
            mock.patch.object(video, "_RECORD_RETRY_MAX_SECONDS", 0.4)
        )
        self.stack.enter_context(
            mock.patch.object(video, "_RECORD_SUCCESS_SECONDS", 0.5)
        )
        self.stack.enter_context(
            mock.patch.object(video, "_RECORD_EXCEPTION_RETRY_SECONDS", 0.1)
        )
        self.stack.enter_context(
            mock.patch.object(
                video,
                "_decode_media_epoch",
                side_effect=lambda *_args, **_kwargs: time.time(),
            )
        )

    def _new_worker(
        self,
        root: Path,
        source_id: str,
        path: str,
    ) -> tuple[VideoWorker, VideoSegmentDB, threading.Thread]:
        db = VideoSegmentDB(root / f"{source_id}.sqlite")
        source = SourceConfig(
            id=source_id,
            name=source_id,
            url=f"rtsp://{RTSP_HOST}:{RTSP_PORT}/{path}",
            rtsp_transport="tcp",
        )
        worker = VideoWorker(source, root / "video", db)
        thread = threading.Thread(
            target=worker.run,
            name=f"fault-{source_id}",
            daemon=True,
        )
        return worker, db, thread

    def _cleanup_worker(self, worker: VideoWorker, thread: threading.Thread) -> None:
        worker.stop()
        thread.join(timeout=10)
        if thread.is_alive():
            raise AssertionError(f"worker {worker.source.id} did not stop")

    def _assert_segment_files_valid(self, db: VideoSegmentDB, video_dir: Path) -> None:
        with db._connect() as conn:
            rows = conn.execute("SELECT path FROM segments ORDER BY id").fetchall()
        self.assertTrue(rows, "expected at least one retained segment")
        for row in rows:
            path = video_dir / row["path"]
            self.assertTrue(path.exists(), f"database references missing {path}")
            self.assertGreater(path.stat().st_size, 0, f"retained empty {path}")
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=codec_name,duration",
                    "-of", "default=nw=1", str(path),
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(
                probe.returncode, 0,
                f"database retains unreadable MP4 {path}: {probe.stderr}",
            )
            self.assertIn("codec_name=h264", probe.stdout)
            self.assertIn("duration=", probe.stdout)
        self.assertFalse(
            [path for path in video_dir.rglob("*.mp4") if path.stat().st_size == 0],
            "zero-byte MP4 files were not cleaned up",
        )

    def test_unavailable_path_uses_exponential_capped_backoff(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wanyard-backoff-fault-") as tmp:
            root = Path(tmp)
            worker, _db, thread = self._new_worker(
                root,
                "missing-camera",
                "fault-path-does-not-exist",
            )
            recorder = _RetryRecordingEvent(retry_threshold=0.15, stop_after=4)
            worker._stop = recorder
            thread.start()
            try:
                thread.join(timeout=15)
            finally:
                self._cleanup_worker(worker, thread)

            self.assertFalse(thread.is_alive(), "backoff worker did not finish")
            self.assertEqual(recorder.retry_delays, [0.2, 0.4, 0.4, 0.4])
            status = worker.status()
            self.assertGreaterEqual(status["consecutive_failures"], 4)
            self.assertEqual(status["last_failure_kind"], "ffmpeg_early_exit")
            self.assertFalse(
                [path for path in (root / "video").rglob("*.mp4") if path.stat().st_size == 0]
            )

    def test_one_source_outage_recovers_without_affecting_other_source(self) -> None:
        publisher_a = _Publisher("fault-source-a", "h264")
        publisher_b = _Publisher("fault-source-b", "h264")
        publisher_a.start()
        publisher_b.start()
        self.addCleanup(publisher_a.stop)
        self.addCleanup(publisher_b.stop)

        with tempfile.TemporaryDirectory(prefix="wanyard-isolation-fault-") as tmp:
            root = Path(tmp)
            worker_a, db_a, thread_a = self._new_worker(
                root, "camera-a", "fault-source-a"
            )
            worker_b, db_b, thread_b = self._new_worker(
                root, "camera-b", "fault-source-b"
            )
            thread_a.start()
            thread_b.start()
            try:
                _wait_for(
                    "both sources to complete a segment",
                    lambda: (
                        worker_a.status()["completed_segments"] >= 1
                        and worker_b.status()["completed_segments"] >= 1
                    ),
                )
                healthy_baseline = worker_b.status()["completed_segments"]

                publisher_a.stop()
                _wait_for(
                    "camera-a outage to be observed",
                    lambda: worker_a.status()["last_failure_kind"] == "ffmpeg_early_exit",
                )
                failure_ts = worker_a.status()["last_failure_ts"]
                self.assertIsNotNone(failure_ts)

                publisher_a = _Publisher("fault-source-a", "h264")
                publisher_a.start()
                self.addCleanup(publisher_a.stop)
                _wait_for(
                    "camera-a to record successfully after recovery",
                    lambda: (
                        worker_a.status()["consecutive_failures"] == 0
                        and (worker_a.status()["segment_completed_ts"] or 0) > failure_ts
                    ),
                )
                _wait_for(
                    "camera-b to keep recording during camera-a outage",
                    lambda: worker_b.status()["completed_segments"] > healthy_baseline,
                )

                self.assertIsNone(worker_b.status()["last_failure_kind"])
            finally:
                self._cleanup_worker(worker_a, thread_a)
                self._cleanup_worker(worker_b, thread_b)
            self._assert_segment_files_valid(db_a, root / "video")
            self._assert_segment_files_valid(db_b, root / "video")

    def test_storage_rotation_keeps_one_archive_rtsp_subscription(self) -> None:
        publisher = _Publisher("persistent-source", "h264")
        publisher.start()
        self.addCleanup(publisher.stop)

        with tempfile.TemporaryDirectory(prefix="wanyard-persistent-fault-") as tmp:
            root = Path(tmp)
            worker, db, thread = self._new_worker(
                root, "persistent-camera", "persistent-source"
            )
            thread.start()
            try:
                _wait_for(
                    "first archive segment to open",
                    lambda: worker._seg_id is not None and worker._proc is not None,
                )
                archive_pid = worker._proc.pid
                _wait_for(
                    "two storage boundaries without reconnect",
                    lambda: worker.status()["completed_segments"] >= 2,
                    timeout=15,
                )
                self.assertIsNotNone(worker._proc)
                self.assertEqual(worker._proc.pid, archive_pid)
                self.assertIsNone(worker.status()["last_failure_kind"])
            finally:
                self._cleanup_worker(worker, thread)

            self._assert_segment_files_valid(db, root / "video")


if __name__ == "__main__":
    unittest.main()
