from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard.config import SourceConfig
from wanyard import sei, video
from wanyard.sei import encode_value
from wanyard.video import VideoWorker, _FFmpegMonitor
from wanyard.video import VideoSegmentDB


class VideoWorkerTests(unittest.TestCase):
    def _worker(self, video_dir: Path, db: mock.Mock | None = None) -> VideoWorker:
        source = SourceConfig(
            id="camera",
            name="Camera",
            url="rtsp://camera/stream",
        )
        if db is None:
            db = mock.Mock()
            db.open_segment_rows.return_value = []   # crash salvage scan
        return VideoWorker(source, video_dir, db)

    def test_mp4_frame_clock_scans_sei_without_decoding(self) -> None:
        class Packet:
            def __init__(self, pts: int, value: int) -> None:
                nal = sei.sei_nal(value)
                self._data = len(nal).to_bytes(4, "big") + nal
                self.pts = pts
                self.time_base = Fraction(1, 1000)
                self.size = len(self._data)

            def __bytes__(self) -> bytes:
                return self._data

        values = [encode_value(1_783_000_000.0 + i * 0.05) for i in range(3)]
        packets = [Packet(100 + i * 50, value) for i, value in enumerate(values)]
        container = mock.Mock()
        container.streams = [types.SimpleNamespace(type="video")]
        container.demux.return_value = packets
        fake_av = types.SimpleNamespace(open=lambda _path: container)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "segment.mp4"
            path.write_bytes(b"mp4")
            with mock.patch.dict(sys.modules, {"av": fake_av}):
                sidecar = video._write_mp4_frame_clock(path)
            assert sidecar is not None
            payload = json.loads(sidecar.read_text(encoding="utf-8"))

        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["frames"], [
            [0.1, values[0]],
            [0.15, values[1]],
            [0.2, values[2]],
        ])
        container.close.assert_called_once()

    def test_mp4_frame_clock_negative_caches_sei_less_files(self) -> None:
        """A SEI-less file writes an EMPTY sidecar once; later calls answer
        from the mtime guard without reopening the media. (Deleting the
        sidecar instead meant every seek into legacy footage re-scanned the
        whole MP4.)"""
        class Packet:
            def __init__(self) -> None:
                self._data = b"\x00\x00\x00\x04\x65\x88\x84\x00"   # slice, no SEI
                self.pts = 100
                self.time_base = Fraction(1, 1000)
                self.size = len(self._data)

            def __bytes__(self) -> bytes:
                return self._data

        container = mock.Mock()
        container.streams = [types.SimpleNamespace(type="video")]
        container.demux.return_value = [Packet()]
        fake_av = types.SimpleNamespace(open=lambda _path: container)

        def _no_reopen(_path):
            raise AssertionError("negative cache must not reopen the media")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "segment.mp4"
            path.write_bytes(b"mp4")
            with mock.patch.dict(sys.modules, {"av": fake_av}):
                sidecar = video._write_mp4_frame_clock(path)
            assert sidecar is not None
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(payload, {"version": 1, "frames": []})

            with mock.patch.dict(
                sys.modules, {"av": types.SimpleNamespace(open=_no_reopen)}
            ):
                again = video._write_mp4_frame_clock(path)
            self.assertEqual(again, sidecar)

    def test_status_reports_h264_stream_contract_without_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            worker = self._worker(Path(tmpdir))
            self.assertEqual(worker.status()["codec"], "h264")

    def test_clean_early_exit_completes_epoch_without_failure_or_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            worker = self._worker(Path(tmpdir))
            proc = mock.Mock()
            proc.poll.return_value = 0
            proc.returncode = 0
            proc.stderr = io.BytesIO(b"input stream ended")

            def start(_ts: float) -> None:
                worker._proc = proc

            def completed(_ts: float, *, reset_failures: bool = True) -> None:
                self.assertTrue(reset_failures)
                worker._stop.set()

            with mock.patch.object(worker, "_start_segment", side_effect=start), \
                 mock.patch.object(worker, "_stop_segment", return_value=True), \
                 mock.patch.object(
                     worker, "_record_segment_completed", side_effect=completed
                 ) as record_completed, \
                 mock.patch.object(worker, "_record_failure") as record_failure, \
                 mock.patch.object(worker._stop, "wait") as wait:
                worker.run()

            record_completed.assert_called_once()
            record_failure.assert_not_called()
            wait.assert_not_called()

    def test_start_segment_copies_stamped_h264(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = mock.Mock()
            db.open_segment.return_value = 7
            worker = self._worker(Path(tmpdir), db)
            proc = mock.Mock()
            with mock.patch("wanyard.capture.resolve_rtsp_url",
                            return_value="rtsp://relay/camera"), \
                 mock.patch("wanyard.video.shutil.which", return_value="/usr/bin/ffmpeg"), \
                 mock.patch.object(worker, "_wait_for_relay_path", return_value=True), \
                 mock.patch("wanyard.video.subprocess.Popen", return_value=proc) as popen:
                worker._start_segment(1_781_600_000.0)

            command = popen.call_args_list[0].args[0]
            self.assertIn("-c:v", command)
            self.assertIn("copy", command)
            self.assertIn("segment", command)
            self.assertIn("-segment_time", command)
            self.assertNotIn("-t", command)
            self.assertEqual(popen.call_count, 2)  # archive + isolated HLS

    def test_ffmpeg_monitor_drains_large_stderr_without_blocking_child(self) -> None:
        payload_bytes = 2 * 1024 * 1024
        code = (
            "import sys;"
            f"sys.stderr.write('x'*{payload_bytes});sys.stderr.flush();"
            "sys.stdout.write('frame=42\\nprogress=end\\n');sys.stdout.flush()"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        monitor = _FFmpegMonitor(proc, "camera", "archive")
        try:
            proc.wait(timeout=5)
        finally:
            monitor.close()

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(monitor.last_frame, 42)
        self.assertEqual(
            len(monitor.diagnostic_tail()), video._RECORD_DIAGNOSTIC_TAIL_BYTES
        )

    def test_ffmpeg_monitor_detects_missing_video_progress(self) -> None:
        proc = mock.Mock(stdout=None, stderr=None)
        monitor = _FFmpegMonitor(proc, "camera", "archive")
        monitor.last_video_progress_wall = time.monotonic() - 30
        self.assertTrue(monitor.video_stalled(20))

    def test_archive_rotation_closes_file_without_restarting_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db = mock.Mock()
            db.open_segment.side_effect = [11, 12]
            worker = self._worker(root, db)
            archive_dir = root / "camera"
            archive_dir.mkdir()
            first = archive_dir / "segment_00000000000000000001.mp4"
            second = archive_dir / "segment_00000000000000000002.mp4"
            first.write_bytes(b"one")
            worker._sync_archive_segments()
            active_proc = mock.Mock()
            worker._proc = active_proc

            second.write_bytes(b"two")
            with mock.patch.object(
                worker, "_close_active_segment", return_value=True
            ) as close_active:
                worker._sync_archive_segments()

            close_active.assert_called_once()
            self.assertIs(worker._proc, active_proc)
            self.assertEqual(worker._seg_id, 12)
            self.assertEqual(worker._seg_path, second)

    def test_sealed_archive_discards_live_detections_beyond_frame_clock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db = VideoSegmentDB(root / "video.sqlite")
            segment_id = db.open_segment("camera", "camera/segment.mp4", 100.0)
            db.insert_live_detections(segment_id, "camera", [
                {"abs_ts": 100.0, "has_human": True, "confidence": .8,
                 "boxes": [], "classes": ["person"]},
                {"abs_ts": 101.0, "has_human": True, "confidence": .7,
                 "boxes": [], "classes": ["person"]},
                {"abs_ts": 100.5, "has_human": True, "confidence": .6,
                 "boxes": [], "classes": ["person"]},
                {"abs_ts": 130.0, "has_human": True, "confidence": .9,
                 "boxes": [], "classes": ["person"]},
            ])
            sidecar = root / "segment.mp4.clock.json"
            sidecar.write_text(json.dumps({
                "version": 1,
                "frames": [[0.0, 10000], [1.0, 10100]],
            }))
            worker = self._worker(root, db)

            removed = worker._discard_unarchived_live_detections(
                segment_id, sidecar
            )

            self.assertEqual(removed, 2)
            self.assertEqual(
                [row["abs_ts"] for row in db.detections_for_segment(segment_id)],
                [100.0, 101.0],
            )

    def test_stop_segment_removes_only_empty_failed_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = mock.Mock()
            worker = self._worker(Path(tmpdir), db)
            segment = Path(tmpdir) / "empty.mp4"
            segment.touch()
            worker._seg_id = 9
            worker._seg_path = segment
            worker._seg_start = 1_781_600_000.0

            self.assertFalse(worker._stop_segment(1_781_600_001.0))

            self.assertFalse(segment.exists())
            db.delete_segment.assert_called_once_with(9)
            db.close_segment.assert_not_called()

    def test_stop_segment_closes_ffmpeg_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            worker = self._worker(Path(tmpdir))
            proc = mock.Mock()
            proc.poll.return_value = 1
            worker._proc = proc

            self.assertFalse(worker._stop_segment(123.0))

            proc.stderr.close.assert_called_once_with()

    def test_status_tracks_failures_and_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            worker = self._worker(Path(tmpdir))
            with mock.patch("wanyard.video.time.time", return_value=123.0):
                worker._record_failure("ffmpeg_early_exit")
            failed = worker.status()
            self.assertEqual(failed["consecutive_failures"], 1)
            self.assertEqual(failed["last_failure_kind"], "ffmpeg_early_exit")
            self.assertEqual(failed["last_failure_ts"], 123.0)

            worker._record_segment_completed(456.0)
            recovered = worker.status()
            self.assertEqual(recovered["consecutive_failures"], 0)
            self.assertEqual(recovered["segment_completed_ts"], 456.0)
            self.assertEqual(recovered["completed_segments"], 1)

    def test_production_retry_defaults_remain_bounded(self) -> None:
        self.assertEqual(video._RECORD_POLL_SECONDS, 1.0)
        self.assertEqual(video._RECORD_RETRY_INITIAL_SECONDS, 5.0)
        self.assertEqual(video._RECORD_RETRY_MAX_SECONDS, 300.0)
        delay = video._RECORD_RETRY_INITIAL_SECONDS
        observed = []
        for _ in range(8):
            observed.append(delay)
            delay = min(delay * 2, video._RECORD_RETRY_MAX_SECONDS)
        self.assertEqual(observed, [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0])


if __name__ == "__main__":
    unittest.main()
