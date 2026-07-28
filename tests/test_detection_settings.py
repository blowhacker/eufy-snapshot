from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard import detection_settings
from wanyard.video import VideoSegmentDB, _parse_results


class DetectionSettingsTests(unittest.TestCase):
    def test_catalog_is_complete_and_defaults_cover_known_confusions(self) -> None:
        self.assertEqual(len(detection_settings.COCO_CLASSES), 80)
        self.assertEqual(
            set(detection_settings.COCO_CLASSES),
            set(range(80)),
        )
        defaults = set(detection_settings.DEFAULT_DETECTION_CLASSES)
        self.assertTrue({"airplane", "kite", "teddy bear"} <= defaults)
        self.assertNotIn("fox", detection_settings.COCO_CLASS_IDS)

    def test_missing_or_corrupt_setting_uses_recommended_defaults(self) -> None:
        expected = list(detection_settings.DEFAULT_DETECTION_CLASSES)
        self.assertEqual(
            detection_settings.normalize_detection_classes(None), expected
        )
        self.assertEqual(
            detection_settings.normalize_detection_classes("{broken"), expected
        )
        self.assertEqual(
            detection_settings.normalize_detection_classes([]), expected
        )

    def test_selection_round_trips_in_coco_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = VideoSegmentDB(Path(tmpdir) / "video.db")
            selected = detection_settings.save_detection_classes(
                db, ["teddy bear", "person", "airplane", "person"]
            )
            self.assertEqual(selected, ["person", "airplane", "teddy bear"])
            self.assertEqual(
                detection_settings.configured_detection_class_ids(db),
                [0, 4, 77],
            )
            stored = db.get_setting(
                detection_settings.DETECTION_CLASSES_SETTING
            )
            self.assertEqual(json.loads(stored), selected)

    def test_invalid_or_empty_selection_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one"):
            detection_settings.validate_detection_classes([])
        with self.assertRaisesRegex(ValueError, "unsupported"):
            detection_settings.validate_detection_classes(["fox"])
        with self.assertRaisesRegex(ValueError, "array"):
            detection_settings.validate_detection_classes("person")

    def test_payload_contains_each_class_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = VideoSegmentDB(Path(tmpdir) / "video.db")
            payload = detection_settings.detection_settings_payload(db)
            names = [
                item["name"]
                for group in payload["groups"]
                for item in group["classes"]
            ]
            self.assertEqual(len(names), 80)
            self.assertEqual(set(names), set(detection_settings.COCO_CLASSES.values()))
            self.assertEqual(
                payload["enabled"],
                list(detection_settings.DEFAULT_DETECTION_CLASSES),
            )

    def test_selector_refreshes_saved_setting_after_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = VideoSegmentDB(Path(tmpdir) / "video.db")
            selector = detection_settings.DetectionClassSelector(
                db, ttl_seconds=0.01
            )
            self.assertIn(4, selector.ids())
            detection_settings.save_detection_classes(db, ["person", "cat"])
            time.sleep(0.12)
            self.assertEqual(selector.ids(), [0, 15])


class _Values:
    def __init__(self, values):
        self.values = values

    def tolist(self):
        return self.values


class _Boxes:
    def __init__(self):
        self.xyxyn = _Values([[0.1, 0.2, 0.3, 0.4]])
        self.conf = _Values([0.91])
        self.cls = _Values([4])

    def __len__(self):
        return 1


class _Result:
    boxes = _Boxes()


class DetectionResultParsingTests(unittest.TestCase):
    def test_complete_catalog_maps_airplane_label(self) -> None:
        has_human, confidence, boxes = _parse_results([_Result()])
        self.assertFalse(has_human)
        self.assertEqual(confidence, 0.0)
        self.assertEqual(boxes[0]["cls"], "airplane")


if __name__ == "__main__":
    unittest.main()
