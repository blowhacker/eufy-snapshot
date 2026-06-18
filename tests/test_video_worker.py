from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard.config import SourceConfig
from wanyard.video import VideoWorker


class VideoWorkerTests(unittest.TestCase):
    def _worker(self, video_dir: Path, db: mock.Mock | None = None) -> VideoWorker:
        source = SourceConfig(
            id="camera",
            name="Camera",
            url="rtsp://camera/stream",
        )
        return VideoWorker(source, video_dir, db or mock.Mock())

    def test_stamped_codec_is_reprobed_after_early_exit_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            worker = self._worker(Path(tmpdir))
            results = [
                subprocess.CompletedProcess([], 0, stdout="hevc\n", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="h264\n", stderr=""),
            ]
            with mock.patch("wanyard.video.shutil.which", return_value="/usr/bin/ffprobe"), \
                 mock.patch("wanyard.video.subprocess.run", side_effect=results) as run:
                self.assertEqual(worker._stamped_codec("rtsp://relay/camera"), "hevc")
                self.assertEqual(worker._stamped_codec("rtsp://relay/camera"), "hevc")
                worker._invalidate_stamped_codec()
                self.assertEqual(worker._stamped_codec("rtsp://relay/camera"), "h264")

            self.assertEqual(run.call_count, 2)
            status = worker.status()
            self.assertEqual(status["codec"], "h264")
            self.assertIsNotNone(status["codec_probe_ts"])

    def test_hvc1_mismatch_is_recognized_for_corrective_retry(self) -> None:
        stderr = (
            "[mp4 @ 0x123] Tag hvc1 incompatible with output codec id '27' (avc1)\n"
            "Could not write header: Invalid data found when processing input"
        )
        self.assertTrue(VideoWorker._is_hvc1_tag_mismatch(stderr))
        self.assertFalse(VideoWorker._is_hvc1_tag_mismatch("connection timed out"))

    def test_run_retries_hvc1_mismatch_once_without_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            worker = self._worker(Path(tmpdir))
            first = mock.Mock()
            first.poll.return_value = 1
            first.returncode = 1
            first.stderr = io.BytesIO(
                b"Tag hvc1 incompatible with output codec id '27' (avc1)"
            )
            second = mock.Mock()
            second.poll.return_value = None
            second.returncode = None
            attempts: list[bool] = []

            def start(_ts: float, *, omit_hvc1: bool = False) -> None:
                attempts.append(omit_hvc1)
                worker._segment_hvc1 = not omit_hvc1
                worker._proc = first if len(attempts) == 1 else second

            def stop(_ts: float) -> bool:
                worker._proc = None
                return False

            def wait(_seconds: float) -> bool:
                worker._stop.set()
                return True

            with mock.patch.object(worker, "_start_segment", side_effect=start), \
                 mock.patch.object(worker, "_stop_segment", side_effect=stop), \
                 mock.patch.object(worker._stop, "wait", side_effect=wait):
                worker.run()

            self.assertEqual(attempts, [False, True])

    def test_start_segment_can_omit_hvc1_for_corrective_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = mock.Mock()
            db.open_segment.return_value = 7
            worker = self._worker(Path(tmpdir), db)
            proc = mock.Mock()
            with mock.patch("wanyard.capture.resolve_rtsp_url",
                            return_value="rtsp://relay/camera"), \
                 mock.patch("wanyard.video.shutil.which", return_value="/usr/bin/ffmpeg"), \
                 mock.patch.object(worker, "_wait_for_relay_path", return_value=True), \
                 mock.patch.object(worker, "_stamped_codec", return_value="hevc"), \
                 mock.patch("wanyard.video.subprocess.Popen", return_value=proc) as popen:
                worker._start_segment(1_781_600_000.0, omit_hvc1=True)

            command = popen.call_args.args[0]
            self.assertNotIn("hvc1", command)
            self.assertFalse(worker._segment_hvc1)

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


if __name__ == "__main__":
    unittest.main()
