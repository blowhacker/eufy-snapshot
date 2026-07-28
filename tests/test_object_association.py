from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard.object_association import ByteTrackAssociator


class FakeDetections:
    def __init__(self, classes, xywh=None, confidence=None):
        self.cls = np.asarray(classes, dtype=np.float32)
        count = len(self.cls)
        self.conf = np.asarray(
            confidence if confidence is not None else [0.8] * count,
            dtype=np.float32,
        )
        self.xywh = np.asarray(
            xywh if xywh is not None else [[10.0, 10.0, 4.0, 8.0]] * count,
            dtype=np.float32,
        ).reshape((-1, 4))

    def __len__(self):
        return len(self.cls)

    def __getitem__(self, indices):
        return FakeDetections(
            self.cls[indices],
            self.xywh[indices],
            self.conf[indices],
        )


class FakeTrack:
    def __init__(self, track_id: int):
        self.track_id = track_id
        self.frame_id = 0
        self.idx = 0


class FakeTracker:
    def __init__(self, args):
        self.args = args
        self.frame_id = 0
        self.tracked_stracks = []
        self.received_xywh = []

    def update(self, detections):
        self.frame_id += 1
        self.received_xywh.append(detections.xywh.copy())
        if len(detections):
            if not self.tracked_stracks:
                self.tracked_stracks = [FakeTrack(1)]
            track = self.tracked_stracks[0]
            track.frame_id = self.frame_id
            track.idx = 0
        return np.empty((0, 8), dtype=np.float32)

    def reset(self):
        self.tracked_stracks = []
        self.frame_id = 0


class IdentityHijackTracker(FakeTracker):
    """Move the second tracker from a tiny false box onto the real person."""

    def update(self, detections):
        self.frame_id += 1
        self.received_xywh.append(detections.xywh.copy())
        if self.frame_id == 1:
            self.tracked_stracks = [FakeTrack(1), FakeTrack(2)]
            for index, track in enumerate(self.tracked_stracks):
                track.frame_id = self.frame_id
                track.idx = index
        else:
            track = self.tracked_stracks[1]
            track.frame_id = self.frame_id
            track.idx = 0
            self.tracked_stracks = [track]
        return np.empty((0, 8), dtype=np.float32)


class FreshTrackEachFrameTracker(FakeTracker):
    """Represent ByteTrack fragmenting a continuously moving subject."""

    def update(self, detections):
        self.frame_id += 1
        self.received_xywh.append(detections.xywh.copy())
        self.tracked_stracks = []
        for index in range(len(detections)):
            track = FakeTrack(self.frame_id * 100 + index)
            track.frame_id = self.frame_id
            track.idx = index
            self.tracked_stracks.append(track)
        return np.empty((0, 8), dtype=np.float32)


class DuplicateHandoffTracker(FakeTracker):
    """Expose old and fresh tracks against the same detection index."""

    def update(self, detections):
        self.frame_id += 1
        self.received_xywh.append(detections.xywh.copy())
        if self.frame_id == 1:
            self.established = FakeTrack(1)
            self.tracked_stracks = [self.established]
        else:
            # Fresh first mirrors the dangerous order; association must still
            # prioritize the already credible identity.
            self.tracked_stracks = [FakeTrack(2), self.established]
        for track in self.tracked_stracks:
            track.frame_id = self.frame_id
            track.idx = 0
        return np.empty((0, 8), dtype=np.float32)


def box(cls: str, confidence: float = 0.8) -> dict:
    return {
        "cls": cls,
        "conf": confidence,
        "x1": 0.1,
        "y1": 0.1,
        "x2": 0.2,
        "y2": 0.3,
    }


class ByteTrackAssociatorTests(unittest.TestCase):
    def make_associator(self):
        return ByteTrackAssociator(
            "front",
            2.0,
            tracker_factory=FakeTracker,
            session_id="test",
        )

    def test_keeps_ephemeral_identity_per_class(self) -> None:
        associator = self.make_associator()
        first = associator.annotate(
            FakeDetections([0, 16]),
            [box("person"), box("dog")],
            abs_ts=100.0,
        )
        second = associator.annotate(
            FakeDetections([0, 16]),
            [box("person"), box("dog")],
            abs_ts=100.5,
        )

        self.assertEqual(len(first), 2)
        self.assertEqual(
            [item["track_id"] for item in first],
            [item["track_id"] for item in second],
        )
        self.assertNotEqual(first[0]["track_id"], first[1]["track_id"])

    def test_resets_identity_after_a_long_input_gap(self) -> None:
        associator = self.make_associator()
        first = associator.annotate(
            FakeDetections([0]), [box("person")], abs_ts=100.0
        )
        after_gap = associator.annotate(
            FakeDetections([0]), [box("person")], abs_ts=107.0
        )

        self.assertNotEqual(first[0]["track_id"], after_gap[0]["track_id"])

    def test_pads_only_tracker_geometry_for_sparse_frames(self) -> None:
        associator = self.make_associator()
        original = [box("person")]
        associator.annotate(
            FakeDetections([0], [[10.0, 20.0, 4.0, 8.0]]),
            original,
            abs_ts=100.0,
        )

        tracker = associator._trackers[0]
        np.testing.assert_allclose(
            tracker.received_xywh[0],
            [[10.0, 20.0, 10.0, 20.0]],
        )
        self.assertEqual(original[0]["x1"], 0.1)
        self.assertFalse(tracker.args.fuse_score)
        self.assertEqual(tracker.args.match_thresh, 0.9)

    def test_fallback_keeps_only_normal_confidence_boxes(self) -> None:
        associator = self.make_associator()
        kept = associator.annotate(
            None,
            [box("person", 0.7), box("dog", 0.2)],
            abs_ts=100.0,
        )

        self.assertEqual([item["cls"] for item in kept], ["person"])

    def test_rescues_real_identity_from_tiny_track_hijack(self) -> None:
        associator = ByteTrackAssociator(
            "front",
            2.0,
            tracker_factory=IdentityHijackTracker,
            session_id="test",
        )
        real = {
            **box("person"),
            "x1": 0.72, "x2": 0.78, "y1": 0.53, "y2": 0.72,
        }
        tiny = {
            **box("person"),
            "x1": 0.63, "x2": 0.64, "y1": 0.23, "y2": 0.28,
        }
        first = associator.annotate(
            FakeDetections(
                [0, 0],
                [[75.0, 62.5, 6.0, 19.0], [63.5, 25.5, 1.0, 5.0]],
            ),
            [real, tiny],
            abs_ts=100.0,
        )
        moved_real = {
            **real,
            "x1": 0.70, "x2": 0.76,
        }
        second = associator.annotate(
            FakeDetections([0], [[73.0, 62.5, 6.0, 19.0]]),
            [moved_real],
            abs_ts=100.5,
        )

        self.assertEqual(second[0]["track_id"], first[0]["track_id"])
        self.assertNotEqual(second[0]["track_id"], first[1]["track_id"])

    def test_reconnects_fresh_track_using_recent_motion(self) -> None:
        associator = ByteTrackAssociator(
            "front",
            2.0,
            tracker_factory=FreshTrackEachFrameTracker,
            session_id="test",
        )
        observed_tokens = []
        for frame, x1 in enumerate((0.72, 0.78, 0.85, 0.91)):
            current = {
                **box("person"),
                "x1": x1,
                "x2": x1 + 0.05,
                "y1": 0.55,
                "y2": 0.73,
            }
            tracked = associator.annotate(
                FakeDetections(
                    [0],
                    [[(x1 + 0.025) * 100, 64.0, 5.0, 18.0]],
                ),
                [current],
                abs_ts=100.0 + frame * 0.56,
            )
            observed_tokens.append(tracked[0]["track_id"])

        self.assertEqual(len(set(observed_tokens)), 1)

    def test_fresh_tracks_do_not_merge_simultaneous_people(self) -> None:
        associator = ByteTrackAssociator(
            "front",
            2.0,
            tracker_factory=FreshTrackEachFrameTracker,
            session_id="test",
        )
        first_boxes = [
            {**box("person"), "x1": 0.40, "x2": 0.45},
            {**box("person"), "x1": 0.48, "x2": 0.53},
        ]
        first = associator.annotate(
            FakeDetections(
                [0, 0],
                [[42.5, 20.0, 5.0, 20.0], [50.5, 20.0, 5.0, 20.0]],
            ),
            first_boxes,
            abs_ts=100.0,
        )
        second_boxes = [
            {**first_boxes[0], "x1": 0.42, "x2": 0.47},
            {**first_boxes[1], "x1": 0.50, "x2": 0.55},
        ]
        second = associator.annotate(
            FakeDetections(
                [0, 0],
                [[44.5, 20.0, 5.0, 20.0], [52.5, 20.0, 5.0, 20.0]],
            ),
            second_boxes,
            abs_ts=100.5,
        )

        self.assertEqual(len({item["track_id"] for item in second}), 2)
        self.assertEqual(
            [item["track_id"] for item in second],
            [item["track_id"] for item in first],
        )

    def test_fresh_handoff_cannot_overwrite_established_assignment(self) -> None:
        associator = ByteTrackAssociator(
            "front",
            2.0,
            tracker_factory=DuplicateHandoffTracker,
            session_id="test",
        )
        first_box = {
            **box("person"),
            "x1": 0.02, "x2": 0.06, "y1": 0.34, "y2": 0.46,
        }
        first = associator.annotate(
            FakeDetections([0], [[4.0, 40.0, 4.0, 12.0]]),
            [first_box],
            abs_ts=100.0,
        )
        second_box = {
            **first_box,
            "x1": 0.04, "x2": 0.08,
        }
        second = associator.annotate(
            FakeDetections([0], [[6.0, 40.0, 4.0, 12.0]]),
            [second_box],
            abs_ts=100.5,
        )

        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["track_id"], first[0]["track_id"])


if __name__ == "__main__":
    unittest.main()
