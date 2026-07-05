from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FRAG_FLAGS = "+frag_keyframe+empty_moov+default_base_moof"


def _ffmpeg(*args: str, wait: bool = True, **kwargs):
    cmd = [shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
           *args]
    if wait:
        return subprocess.run(cmd, capture_output=True, timeout=60, **kwargs)
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, **kwargs)


def _make_source(path: Path, seconds: int = 60) -> None:
    """Pre-encoded h264 elementary stream shaped like a camera: bf=0, 1s GOP."""
    _ffmpeg("-f", "lavfi", "-i", "testsrc=rate=10:size=320x240",
            "-t", str(seconds), "-c:v", "libx264", "-bf", "0", "-g", "10",
            "-f", "h264", str(path))


def _write_frag(path: Path, seconds: int = 4, *, movflags: str = FRAG_FLAGS):
    src = path.with_name(path.name + ".src.264")
    _make_source(src, seconds)
    _ffmpeg("-r", "10", "-i", str(src), "-c:v", "copy",
            "-movflags", movflags, "-f", "mp4", str(path))


def _spawn_realtime_writer(path: Path, *, movflags: str = FRAG_FLAGS):
    """ffmpeg stream-copying into the file in real time — the recorder's shape
    (copy, no encoder buffering: packets hit the muxer as they arrive)."""
    src = path.with_name(path.name + ".src.264")
    _make_source(src)
    return _ffmpeg("-re", "-r", "10", "-i", str(src), "-c:v", "copy",
                   "-movflags", movflags, "-flush_packets", "1",
                   "-f", "mp4", str(path), wait=False)


def _decode_count(path: Path) -> int:
    import av

    n = 0
    with av.open(str(path)) as c:
        for _ in c.decode(video=0):
            n += 1
    return n


def _top_level_boxes(path: Path) -> list[str]:
    boxes = []
    with open(path, "rb") as f:
        while True:
            header = f.read(8)
            if len(header) < 8:
                break
            size = int.from_bytes(header[:4], "big")
            boxes.append(header[4:8].decode("latin1"))
            if size == 1:
                size = int.from_bytes(f.read(8), "big")
                f.seek(size - 16, os.SEEK_CUR)
            elif size == 0:
                break
            else:
                f.seek(size - 8, os.SEEK_CUR)
    return boxes


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available")
class OpenFileBehaviourTests(unittest.TestCase):
    def test_open_file_is_readable_and_seekable_while_writing(self) -> None:
        import av

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "open.mp4"
            proc = _spawn_realtime_writer(path)
            try:
                time.sleep(5.0)
                # readable mid-write
                with av.open(str(path)) as c:
                    frames = [f for f in c.decode(video=0)]
                self.assertGreater(len(frames), 10)
                # seekable mid-write: land midway without reading from zero
                mid = frames[len(frames) // 2]
                with av.open(str(path)) as c:
                    stream = c.streams.video[0]
                    c.seek(mid.pts, stream=stream)
                    landed = next(c.decode(video=0))
                    self.assertGreaterEqual(landed.pts, 0)
                    self.assertLessEqual(
                        abs(float((landed.pts - mid.pts) * stream.time_base)),
                        1.0,   # within one GOP of the target
                    )
            finally:
                proc.kill()
                proc.wait()

    def test_crash_loses_at_most_a_tail_not_the_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "crash.mp4"
            proc = _spawn_realtime_writer(path)
            time.sleep(5.0)
            proc.send_signal(signal.SIGKILL)   # no cleanup, no trailer
            proc.wait()
            n = _decode_count(path)
            # ~50 frames written at 10fps; everything up to the last complete
            # fragment must survive (>= 3s of the ~5s written).
            self.assertGreaterEqual(n, 30)

    def test_control_faststart_crash_loses_everything(self) -> None:
        """The old format: same crash, zero recoverable frames — the reason
        for this change. If ffmpeg ever makes killed faststart files readable
        this test tells us the remux-at-close step is the only remaining
        reason to keep the finalize pass."""
        import av

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "crash-faststart.mp4"
            proc = _spawn_realtime_writer(path, movflags="+faststart")
            time.sleep(5.0)
            proc.send_signal(signal.SIGKILL)
            proc.wait()
            with self.assertRaises(av.error.InvalidDataError):
                _decode_count(path)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available")
class FinalizeTests(unittest.TestCase):
    def test_finalize_produces_faststart_with_identical_frames(self) -> None:
        import av

        from wanyard.video import _finalize_segment_file

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "seg.mp4"
            _write_frag(path)
            before_n = _decode_count(path)
            with av.open(str(path)) as c:
                before_first = next(c.decode(video=0)).pts
            self.assertIn("moof", _top_level_boxes(path))

            self.assertTrue(_finalize_segment_file(path))

            boxes = _top_level_boxes(path)
            self.assertNotIn("moof", boxes)
            self.assertLess(boxes.index("moov"), boxes.index("mdat"),
                            "faststart: moov must precede mdat")
            self.assertEqual(_decode_count(path), before_n)
            with av.open(str(path)) as c:
                self.assertEqual(next(c.decode(video=0)).pts, before_first)

    def test_finalize_of_crashed_file_keeps_surviving_frames(self) -> None:
        from wanyard.video import _finalize_segment_file

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "crashed.mp4"
            proc = _spawn_realtime_writer(path)
            time.sleep(4.0)
            proc.send_signal(signal.SIGKILL)
            proc.wait()
            survivors = _decode_count(path)
            self.assertTrue(_finalize_segment_file(path))
            self.assertEqual(_decode_count(path), survivors)
            boxes = _top_level_boxes(path)
            self.assertLess(boxes.index("moov"), boxes.index("mdat"))


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available")
class SalvageTests(unittest.TestCase):
    def _worker(self, tmpdir: Path):
        from wanyard.video import VideoSegmentDB, VideoWorker

        db = VideoSegmentDB(tmpdir / "video.db")
        source = SimpleNamespace(id="cam")
        worker = VideoWorker(source, tmpdir, db)
        return worker, db

    def test_salvage_closes_crashed_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            worker, db = self._worker(tmpdir)

            rel = "cam/crashed.mp4"
            path = tmpdir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            proc = _spawn_realtime_writer(path)
            time.sleep(4.0)
            proc.send_signal(signal.SIGKILL)
            proc.wait()
            survivors = _decode_count(path)
            self.assertGreater(survivors, 0)

            start = time.time() - 120
            seg_id = db.open_segment("cam", rel, start)
            old_mtime = time.time() - 60      # idle long enough to salvage
            os.utime(path, (old_mtime, old_mtime))

            worker._salvage_orphan_segments()

            with db._connect() as conn:
                row = dict(conn.execute(
                    "SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone())
            self.assertIsNotNone(row["end_ts"])
            self.assertAlmostEqual(row["end_ts"], old_mtime, delta=1.0)
            self.assertIsNotNone(row["scanned_at"])   # no clock -> backfill kept away
            # footage survived salvage + finalize
            self.assertEqual(_decode_count(path), survivors)

    def test_salvage_skips_recent_files_and_deletes_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            worker, db = self._worker(tmpdir)

            fresh_rel = "cam/fresh.mp4"
            fresh = tmpdir / fresh_rel
            fresh.parent.mkdir(parents=True, exist_ok=True)
            _write_frag(fresh, seconds=1)
            fresh_id = db.open_segment("cam", fresh_rel, time.time())

            gone_id = db.open_segment("cam", "cam/gone.mp4", time.time() - 900)

            worker._salvage_orphan_segments()

            open_ids = {r["id"] for r in db.open_segment_rows("cam")}
            self.assertIn(fresh_id, open_ids)      # recent: left alone
            self.assertNotIn(gone_id, open_ids)    # fileless: row deleted
            with db._connect() as conn:
                self.assertIsNone(conn.execute(
                    "SELECT id FROM segments WHERE id=?", (gone_id,)).fetchone())


if __name__ == "__main__":
    unittest.main()
