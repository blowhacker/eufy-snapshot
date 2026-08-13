from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard.search import plan_search, summarize_search
from wanyard.video import VideoSegmentDB
from wanyard.visual_search import (
    VisualSearchError,
    VisualObservationStore,
    normalize_observation,
    observation_matches,
)


SOURCES = [
    {"id": "garden-old", "name": "Garden old"},
    {"id": "front", "name": "Front"},
]


class SearchPlannerTests(unittest.TestCase):
    now = datetime(2026, 8, 13, 14, 30)

    def test_black_cat_yesterday_is_auditable_candidate_search(self) -> None:
        plan = plan_search(
            "Was the black cat spotted yesterday?", SOURCES, now=self.now
        )

        self.assertEqual(plan.subject, "cat")
        self.assertEqual(plan.classes, ("cat",))
        self.assertEqual(plan.visual_requirements, ("black",))
        self.assertEqual(plan.time_label, "yesterday")
        self.assertEqual(
            datetime.fromtimestamp(plan.since), datetime(2026, 8, 12)
        )
        answer, confidence = summarize_search(plan, [{"id": 1}])
        self.assertIn("not been visually verified", answer)
        self.assertEqual(confidence, "candidates")

    def test_fox_retrieves_detector_confusions_for_visual_review(self) -> None:
        plan = plan_search(
            "Did a fox visit garden old last night?", SOURCES, now=self.now
        )

        self.assertEqual(plan.classes, ("cat", "dog"))
        self.assertEqual(plan.source_ids, ("garden-old",))
        self.assertIn("species", plan.visual_requirements)

    def test_bins_move_requires_scene_index(self) -> None:
        plan = plan_search("What time did the bins move?", SOURCES, now=self.now)
        answer, confidence = summarize_search(plan, [])

        self.assertEqual(plan.intent, "scene_change")
        self.assertEqual(plan.classes, ())
        self.assertIn("scene-state index", answer)
        self.assertEqual(confidence, "not indexed")


class SearchEvidenceTests(unittest.TestCase):
    def test_searches_finalized_events_by_sources_classes_and_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = VideoSegmentDB(Path(tmpdir) / "video.db")
            front = db.open_segment("front", "front/a.mp4", 100.0)
            garden = db.open_segment("garden-old", "garden-old/a.mp4", 100.0)
            for segment in (front, garden):
                db.set_segment_media_start(segment, 100.0)
                db.close_segment(segment, 160.0, None, None)
            base = {
                "start_off": 1.0,
                "end_off": 3.0,
                "confidence": 0.8,
                "boxes_json": json.dumps([]),
                "event_type": "detection",
                "track_id": None,
            }
            db.insert_events([
                {**base, "segment_id": front, "source_id": "front", "abs_ts": 110.0, "class": "person"},
                {**base, "segment_id": garden, "source_id": "garden-old", "abs_ts": 120.0, "class": "cat"},
                {**base, "segment_id": garden, "source_id": "garden-old", "abs_ts": 130.0, "class": "dog"},
                {**base, "segment_id": garden, "source_id": "garden-old", "abs_ts": 140.0, "class": "car"},
            ])

            rows = db.search_detection_events(
                ["garden-old"], ["cat", "dog"], 115.0, 135.0
            )

        self.assertEqual(
            [(row["class"], row["abs_ts"]) for row in rows],
            [("dog", 130.0), ("cat", 120.0)],
        )

    def test_visual_observations_are_versioned_and_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = VisualObservationStore(Path(tmpdir) / "search.db")
            observation = {
                "subject": "fox",
                "colours": ["red", "white"],
                "action": "walking",
                "description": "A fox walking across grass.",
                "confidence": 0.86,
            }
            store.put("42", "vision-model", "prompt-v1", observation)

            cached = store.get("42", "vision-model", "prompt-v1")
            other_version = store.get("42", "vision-model", "prompt-v2")

        self.assertEqual(cached["subject"], "fox")
        self.assertIn("created_at", cached)
        self.assertIsNone(other_version)


class VisualObservationTests(unittest.TestCase):
    def test_normalizes_untrusted_model_output(self) -> None:
        observation = normalize_observation({
            "subject": "FOX",
            "colours": [" Red ", "red", "White"],
            "action": "walking",
            "description": "Likely fox",
            "confidence": 1.4,
        })

        self.assertEqual(observation["subject"], "fox")
        self.assertEqual(observation["colours"], ["red", "white"])
        self.assertEqual(observation["confidence"], 1.0)
        self.assertTrue(observation_matches("fox", ("species",), observation))
        self.assertFalse(observation_matches("cat", ("black",), observation))

    def test_rejects_non_object_observation(self) -> None:
        with self.assertRaises(VisualSearchError):
            normalize_observation(["fox"])


if __name__ == "__main__":
    unittest.main()
