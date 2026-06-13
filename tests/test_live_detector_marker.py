from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard import bitc
from wanyard.live_detector import _SourceWorker


class _FakeFrame:
    def __init__(self, frame_bgr: np.ndarray) -> None:
        self.frame_bgr = frame_bgr

    def to_ndarray(self, *, format: str):
        if format != "bgr24":
            raise AssertionError(format)
        return self.frame_bgr.copy()


class _FakeModel:
    def __init__(self) -> None:
        self.frames: list[np.ndarray] = []

    def predict(self, frame_bgr, **_kwargs):
        self.frames.append(frame_bgr.copy())
        return ["unused"]


class _FakeVideoDB:
    def __init__(self, segment: dict | None = None) -> None:
        self.segment = segment
        self.inserted: list[tuple[int, str, list[dict]]] = []
        self.marked: list[int] = []
        self.closed: dict[int, dict] = {}

    def open_live_segment(self, _source_id: str):
        return self.segment

    def insert_live_detections(self, segment_id: int, source_id: str, rows: list[dict]):
        self.inserted.append((segment_id, source_id, rows))

    def get_segment(self, segment_id: int):
        return self.closed.get(segment_id)

    def mark_scanned(self, segment_id: int):
        self.marked.append(segment_id)

    def detections_for_segment(self, _segment_id: int):
        return []


def _worker(model: _FakeModel, video_db: _FakeVideoDB) -> _SourceWorker:
    return _SourceWorker(
        "mediamtx",
        "tapo-front",
        "tapo-front-stamped",
        model,
        video_db,
        threading.Event(),
        threading.Lock(),
        fps=100.0,
        claim=True,
    )


class LiveDetectorMarkerTests(unittest.TestCase):
    def test_inference_uses_marker_abs_ts_and_masks_strip(self) -> None:
        unix_seconds = 1_781_400_000.12
        frame = np.full((64, bitc.WIDTH + 32, 3), 180, dtype=np.uint8)
        bitc.render(frame, bitc.encode_value(unix_seconds))
        model = _FakeModel()
        video_db = _FakeVideoDB({"id": 7, "media_epoch": unix_seconds - 0.2})
        worker = _worker(model, video_db)

        with mock.patch("wanyard.video._parse_results", return_value=(False, 0.0, [])):
            worker._maybe_infer(_FakeFrame(frame), unix_seconds + 0.1)

        self.assertEqual(len(model.frames), 1)
        x0, y0, width, height = bitc.geometry(model.frames[0])
        self.assertTrue(np.all(model.frames[0][y0 : y0 + height, x0 : x0 + width, :] == 0))
        self.assertEqual(len(video_db.inserted), 1)
        segment_id, source_id, rows = video_db.inserted[0]
        self.assertEqual(segment_id, 7)
        self.assertEqual(source_id, "tapo-front")
        self.assertEqual(round(rows[0]["abs_ts"] * 100), bitc.encode_value(unix_seconds))
        self.assertEqual(worker.marker_active_since, rows[0]["abs_ts"])

    def test_crc_failure_skips_prediction_and_storage(self) -> None:
        unix_seconds = 1_781_400_000.12
        frame = np.full((64, bitc.WIDTH + 32, 3), 180, dtype=np.uint8)
        model = _FakeModel()
        video_db = _FakeVideoDB({"id": 7, "media_epoch": unix_seconds - 0.2})
        worker = _worker(model, video_db)

        with mock.patch("wanyard.video._parse_results", return_value=(False, 0.0, [])):
            worker._maybe_infer(_FakeFrame(frame), unix_seconds + 0.1)

        self.assertEqual(model.frames, [])
        self.assertEqual(video_db.inserted, [])
        self.assertIsNone(worker.marker_active_since)

    def test_rotation_claim_uses_marker_coverage(self) -> None:
        model = _FakeModel()
        video_db = _FakeVideoDB({"id": 2, "media_epoch": 1001.0})
        video_db.closed[1] = {"id": 1, "media_epoch": 1000.0, "end_ts": 1010.0}
        worker = _worker(model, video_db)
        worker.open_segment_id = 1
        worker.marker_active_since = 999.9

        with mock.patch("wanyard.video.extract_events", return_value=0):
            worker._handle_rotation(video_db.segment)

        self.assertEqual(video_db.marked, [1])
        self.assertEqual(worker.open_segment_id, 2)


if __name__ == "__main__":
    unittest.main()
