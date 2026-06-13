from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard import bitc
from wanyard.video import VideoSegmentDB, _decode_first_frame_marker


class _FakeFrame:
    def __init__(self, frame_bgr: np.ndarray) -> None:
        self.frame_bgr = frame_bgr

    def to_ndarray(self, *, format: str):
        if format != "bgr24":
            raise AssertionError(format)
        return self.frame_bgr.copy()


class _FakePacket:
    def __init__(self, frames: list[_FakeFrame]) -> None:
        self._frames = frames

    def decode(self):
        return list(self._frames)


class _FakeContainer:
    def __init__(self, frames: list[np.ndarray], *, has_video: bool = True) -> None:
        self.streams = []
        if has_video:
            self.streams.append(types.SimpleNamespace(type="video"))
        self._packets = [_FakePacket([_FakeFrame(frame) for frame in frames])]
        self.closed = False

    def demux(self, _stream):
        return list(self._packets)

    def close(self) -> None:
        self.closed = True


class VideoBitcAnchorTests(unittest.TestCase):
    def test_decode_first_frame_marker_reads_first_video_frame(self) -> None:
        unix_seconds = 1_781_600_000.12
        first = np.full((64, bitc.WIDTH + 16, 3), 180, dtype=np.uint8)
        second = np.full_like(first, 40)
        bitc.render(first, bitc.encode_value(unix_seconds))
        bitc.render(second, bitc.encode_value(unix_seconds + 10.0))
        container = _FakeContainer([first, second])

        fake_av = types.SimpleNamespace(open=lambda _path: container)
        with mock.patch.dict(sys.modules, {"av": fake_av}):
            decoded = _decode_first_frame_marker("segment.mp4")

        self.assertIsNotNone(decoded)
        self.assertEqual(round(decoded * 100), bitc.encode_value(unix_seconds))
        self.assertTrue(container.closed)

    def test_decode_first_frame_marker_returns_none_without_valid_marker(self) -> None:
        frame = np.full((64, bitc.WIDTH + 16, 3), 180, dtype=np.uint8)
        container = _FakeContainer([frame])

        fake_av = types.SimpleNamespace(open=lambda _path: container)
        with mock.patch.dict(sys.modules, {"av": fake_av}):
            decoded = _decode_first_frame_marker("segment.mp4")

        self.assertIsNone(decoded)
        self.assertTrue(container.closed)

    def test_decode_first_frame_marker_returns_none_without_video(self) -> None:
        container = _FakeContainer([], has_video=False)

        fake_av = types.SimpleNamespace(open=lambda _path: container)
        with mock.patch.dict(sys.modules, {"av": fake_av}):
            decoded = _decode_first_frame_marker("audio-only.mp4")

        self.assertIsNone(decoded)
        self.assertTrue(container.closed)

    def test_segment_media_start_is_exact_assignment(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wanyard-video-db-") as tmp:
            db = VideoSegmentDB(Path(tmp) / "video.sqlite")
            segment_id = db.open_segment(
                "front",
                "front/2026/06/13/segment.mp4",
                1_781_600_000.0,
            )

            db.set_segment_media_start(segment_id, 1_781_600_001.0)
            db.set_segment_media_start(segment_id, 1_781_600_002.0)

            segment = db.get_segment(segment_id)
            assert segment is not None
            self.assertEqual(segment["media_epoch"], 1_781_600_002.0)


if __name__ == "__main__":
    unittest.main()
