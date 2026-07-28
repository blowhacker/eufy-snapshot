from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard.video import (
    VideoSegmentDB,
    _object_tracklets_from_detections,
    extract_events,
)


def box(
    cx: float,
    *,
    track_id: str | None = None,
    confidence: float = 0.8,
) -> dict:
    value = {
        "cls": "person",
        "conf": confidence,
        "x1": cx - 0.04,
        "y1": 0.3,
        "x2": cx + 0.04,
        "y2": 0.7,
    }
    if track_id:
        value["track_id"] = track_id
    return value


def detection(
    offset: float,
    cx: float,
    *,
    track_id: str | None = None,
    confidence: float = 0.8,
) -> dict:
    return {
        "ts_offset": offset,
        "abs_ts": 100.0 + offset,
        "boxes": [
            box(cx, track_id=track_id, confidence=confidence)
        ],
    }


class ObjectTrackletTests(unittest.TestCase):
    def test_bytetrack_identity_joins_a_fast_walking_subject(self) -> None:
        segment = {"id": 1, "source_id": "front", "media_epoch": 100.0}
        detections = [
            detection(1.0, 0.10, track_id="front:session:1"),
            detection(1.5, 0.22, track_id="front:session:1"),
            detection(2.0, 0.34, track_id="front:session:1"),
            detection(2.5, 0.46, track_id="front:session:1"),
            detection(3.0, 0.58, track_id="front:session:1"),
        ]

        rows = _object_tracklets_from_detections(segment, detections)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["observations"], 5)
        self.assertEqual(rows[0]["start_off"], 1.0)
        self.assertEqual(rows[0]["end_off"], 3.0)
        self.assertEqual(
            json.loads(rows[0]["boxes_json"])[0]["track_id"],
            "front:session:1",
        )

    def test_geometry_alone_splits_the_same_fast_motion(self) -> None:
        segment = {"id": 1, "source_id": "front", "media_epoch": 100.0}
        detections = [
            detection(1.0, 0.10),
            detection(1.5, 0.22),
            detection(2.0, 0.34),
            detection(2.5, 0.46),
            detection(3.0, 0.58),
        ]

        self.assertEqual(
            _object_tracklets_from_detections(segment, detections),
            [],
        )

    def test_representative_box_and_time_are_same_observation(self) -> None:
        segment = {"id": 1, "source_id": "front", "media_epoch": 100.0}
        token = "front:session:1"
        detections = [
            detection(1.0, 0.10, track_id=token, confidence=0.60),
            detection(1.5, 0.40, track_id=token, confidence=0.95),
            detection(2.0, 0.70, track_id=token, confidence=0.75),
        ]

        row = _object_tracklets_from_detections(segment, detections)[0]
        representative = json.loads(row["boxes_json"])[0]
        first = json.loads(row["first_boxes_json"])[0]
        last = json.loads(row["last_boxes_json"])[0]

        self.assertEqual(row["abs_ts"], 101.5)
        self.assertEqual(row["start_off"], 1.5)
        self.assertAlmostEqual(
            (representative["x1"] + representative["x2"]) / 2,
            0.40,
        )
        self.assertEqual(row["track_first_abs_ts"], 101.0)
        self.assertAlmostEqual((first["x1"] + first["x2"]) / 2, 0.10)
        self.assertEqual(row["track_last_abs_ts"], 102.0)
        self.assertAlmostEqual((last["x1"] + last["x2"]) / 2, 0.70)

    def test_identity_continues_an_episode_across_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = VideoSegmentDB(Path(tmpdir) / "video.db")
            first_id = db.open_segment("front", "front/one.mp4", 100.0)
            db.set_segment_media_start(first_id, 100.0)
            db.close_segment(first_id, 110.0, None, None)
            second_id = db.open_segment("front", "front/two.mp4", 110.0)
            db.set_segment_media_start(second_id, 110.0)
            db.close_segment(second_id, 120.0, None, None)
            first = db.get_segment(first_id)
            second = db.get_segment(second_id)
            token = "front:session:1"

            first_events = extract_events(first, [
                detection(8.5, 0.10, track_id=token),
                detection(9.0, 0.22, track_id=token),
            ], db)
            second_events = extract_events(second, [
                {
                    **detection(10.5, 0.78, track_id=token),
                    "ts_offset": 0.5,
                    "abs_ts": 110.5,
                },
                {
                    **detection(11.0, 0.90, track_id=token),
                    "ts_offset": 1.0,
                    "abs_ts": 111.0,
                },
            ], db)

            self.assertEqual(first_events, 1)
            self.assertEqual(second_events, 0)
            self.assertEqual(
                len(db.list_detection_events("front", ["person"], 10, None)),
                1,
            )


if __name__ == "__main__":
    unittest.main()
