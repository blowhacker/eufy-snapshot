from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard.web import (
    _detection_wall_all,
    _detection_wall_camera,
    _detection_wall_preview,
    _gzip_path_is_excluded,
    make_app,
)
from wanyard.config import AppConfig, SourceConfig
from wanyard.video import (
    VideoSegmentDB,
    _encounter_events_from_detections,
    _filter_with_polygons,
    _filter_with_zone_policy,
    _merge_encounter_area_boxes,
)


def _event(
    event_id,
    ts: float,
    cls: str,
    *,
    source_id: str = "front",
    event_type: str = "detection",
    provisional: bool = False,
    seg_path: str | None = None,
    boxes: list[dict] | None = None,
) -> dict:
    return {
        "id": event_id,
        "source_id": source_id,
        "abs_ts": ts,
        "display_ts": ts,
        "class": cls,
        "event_type": event_type,
        "start_off": 1.0,
        "end_off": 4.0,
        "confidence": 0.87,
        "provisional": provisional,
        "seg_path": seg_path,
        "boxes_json": json.dumps(boxes) if boxes is not None else None,
    }


def _box(cx: float, cy: float, cls: str = "person") -> dict:
    return {
        "cls": cls,
        "x1": cx - 0.02,
        "y1": cy - 0.02,
        "x2": cx + 0.02,
        "y2": cy + 0.02,
    }


def _area(x1: float, y1: float, x2: float, y2: float) -> list[dict]:
    return [
        {"x": x1, "y": y1},
        {"x": x2, "y": y1},
        {"x": x2, "y": y2},
        {"x": x1, "y": y2},
    ]


class FakeVideoDB:
    def __init__(self, recorded: list[dict], provisional: list[dict] | None = None):
        self.recorded = recorded
        self.provisional = provisional or []
        self.calls = []

    def list_detection_events(self, source_id, classes, limit, before):
        self.calls.append({
            "source_id": source_id,
            "classes": classes,
            "limit": limit,
            "before": before,
        })
        rows = [
            event for event in self.recorded
            if event["source_id"] == source_id
            and (not classes or event["class"] in classes)
            and event.get("event_type") != "disappeared"
            and (before is None or event["display_ts"] < before)
        ]
        return sorted(rows, key=lambda event: event["display_ts"], reverse=True)[:limit]

    def provisional_events(self, source_id):
        self.calls.append({"provisional_source_id": source_id})
        return [
            event for event in self.provisional
            if event["source_id"] == source_id
        ]


class DetectionWallCameraTests(unittest.TestCase):
    source = {
        "id": "front",
        "name": "Front door",
        "record_mode": "continuous",
    }

    def test_combines_selected_classes_newest_first(self) -> None:
        db = FakeVideoDB([
            _event(1, 100.0, "person"),
            _event(2, 103.0, "dog"),
            _event(3, 102.0, "car"),
            _event(4, 101.0, "person"),
        ])

        camera = _detection_wall_camera(
            db, self.source, ["person", "car"], limit=8, before=None
        )

        self.assertEqual(
            [(event["id"], event["class"]) for event in camera["events"]],
            [("3", "car"), ("4", "person"), ("1", "person")],
        )
        self.assertEqual(db.calls[0]["classes"], ["person", "car"])

    def test_excludes_disappearance_rows_and_future_cursor_rows(self) -> None:
        db = FakeVideoDB([
            _event("o:1", 110.0, "person"),
            _event("o:2", 105.0, "person", event_type="disappeared"),
            _event("o:3", 99.0, "person"),
        ])

        camera = _detection_wall_camera(
            db, self.source, [], limit=8, before=105.0
        )

        self.assertEqual([event["id"] for event in camera["events"]], ["o:3"])

    def test_deduplicates_provisional_and_builds_public_urls(self) -> None:
        duplicate = _event("p:7:person:1.0", 200.0, "person", provisional=True)
        db = FakeVideoDB(
            [_event(9, 190.0, "person")],
            provisional=[duplicate, dict(duplicate)],
        )

        camera = _detection_wall_camera(
            db, self.source, [], limit=8, before=None
        )

        self.assertEqual(len(camera["events"]), 2)
        event = camera["events"][0]
        self.assertEqual(
            event["thumb_url"],
            "/api/video/event-thumb/p%3A7%3Aperson%3A1.0",
        )
        self.assertIn("source=front", event["target_url"])
        self.assertIn("ts=200.000", event["target_url"])
        self.assertIn("cls=person", event["target_url"])
        self.assertTrue(event["provisional"])

    def test_pagination_cursor_is_source_local_and_exclusive(self) -> None:
        db = FakeVideoDB([
            _event(index, float(200 - index), "person")
            for index in range(12)
        ])

        camera = _detection_wall_camera(
            db, self.source, [], limit=8, before=None
        )

        self.assertEqual(len(camera["events"]), 8)
        self.assertAlmostEqual(
            camera["next_before"],
            camera["events"][-1]["display_ts"] - 0.000001,
        )

        next_camera = _detection_wall_camera(
            db, self.source, [], limit=8, before=camera["next_before"]
        )
        first_ids = {event["id"] for event in camera["events"]}
        next_ids = {event["id"] for event in next_camera["events"]}
        self.assertFalse(first_ids & next_ids)

    def test_multiple_areas_use_union_semantics(self) -> None:
        db = FakeVideoDB([
            _event(1, 103.0, "person", boxes=[_box(0.15, 0.15)]),
            _event(2, 102.0, "person", boxes=[_box(0.85, 0.85)]),
            _event(3, 101.0, "person", boxes=[_box(0.50, 0.50)]),
        ])

        camera = _detection_wall_camera(
            db,
            self.source,
            [],
            limit=8,
            before=None,
            polygons=[
                _area(0.0, 0.0, 0.3, 0.3),
                _area(0.7, 0.7, 1.0, 1.0),
            ],
            zone_filter_active=True,
        )

        self.assertEqual([event["id"] for event in camera["events"]], ["1", "2"])

    def test_active_area_filter_without_source_area_returns_no_events(self) -> None:
        db = FakeVideoDB([
            _event(1, 100.0, "person", boxes=[_box(0.5, 0.5)]),
        ])

        camera = _detection_wall_camera(
            db,
            self.source,
            [],
            limit=8,
            before=None,
            polygons=None,
            zone_filter_active=True,
        )

        self.assertEqual(camera["events"], [])

    def test_area_filter_scans_past_dense_nonmatching_pages(self) -> None:
        db = FakeVideoDB([
            *[
                _event(index, 500.0 - index, "person", boxes=[_box(0.8, 0.8)])
                for index in range(205)
            ],
            _event("older-match", 200.0, "person", boxes=[_box(0.1, 0.1)]),
        ])

        camera = _detection_wall_camera(
            db,
            self.source,
            [],
            limit=8,
            before=None,
            polygons=[_area(0.0, 0.0, 0.3, 0.3)],
            zone_filter_active=True,
        )

        self.assertEqual([event["id"] for event in camera["events"]], ["older-match"])
        recorded_calls = [call for call in db.calls if "source_id" in call]
        self.assertGreaterEqual(len(recorded_calls), 2)

    def test_exposes_frontend_crop_preview_for_closed_mp4(self) -> None:
        event = _event(
            17,
            100.0,
            "person",
            seg_path="front/segment one.mp4",
            boxes=[
                {
                    "cls": "car",
                    "conf": 0.99,
                    "x1": 0.1,
                    "y1": 0.1,
                    "x2": 0.9,
                    "y2": 0.9,
                },
                {
                    "cls": "person",
                    "conf": 0.85,
                    "x1": 0.2,
                    "y1": 0.25,
                    "x2": 0.4,
                    "y2": 0.75,
                },
                {
                    "cls": "person",
                    "conf": 0.99,
                    "x1": 0.7,
                    "y1": 0.1,
                    "x2": 0.9,
                    "y2": 0.4,
                    "_zone_sample": True,
                },
            ],
        )
        event["start_off"] = 6.0
        event["end_off"] = 10.0

        db = FakeVideoDB([event])
        camera = _detection_wall_camera(
            db, self.source, ["person"], limit=8, before=None
        )
        preview = camera["events"][0]["preview"]

        self.assertEqual(
            preview,
            {
                "url": "/video/files/front/segment%20one.mp4",
                "source_id": "front",
                "class": "person",
                "event_ts": 100.0,
                "start_ts": 99.0,
                "end_ts": 105.0,
                "start": 5.0,
                "end": 11.0,
                "box": {
                    "x1": 0.2,
                    "y1": 0.25,
                    "x2": 0.4,
                    "y2": 0.75,
                },
            },
        )

    def test_exposes_hls_preview_for_provisional_event_in_live_window(self) -> None:
        provisional = _event(
            18,
            100.0,
            "person",
            provisional=True,
            seg_path="front/segment.mp4",
            boxes=[{"cls": "person", "x1": 0.2, "y1": 0.2, "x2": 0.4, "y2": 0.5}],
        )

        preview = _detection_wall_preview(
            provisional,
            "person",
            {"start_ts": 95.0, "end_ts": 110.0},
        )

        self.assertEqual(
            preview,
            {
                "kind": "hls",
                "url": "/video/live/front/live.m3u8",
                "source_id": "front",
                "class": "person",
                "event_ts": 100.0,
                "start_ts": 99.0,
                "end_ts": 104.0,
                "box": {
                    "x1": 0.2,
                    "y1": 0.2,
                    "x2": 0.4,
                    "y2": 0.5,
                },
            },
        )

    def test_preview_follows_long_encounter_with_safety_cap(self) -> None:
        recorded = _event(
            19,
            100.0,
            "person",
            seg_path="front/long-walk.mp4",
            boxes=[{"cls": "person", "x1": 0.2, "y1": 0.2, "x2": 0.4, "y2": 0.5}],
        )
        recorded["start_off"] = 20.0
        recorded["end_off"] = 93.8

        preview = _detection_wall_preview(recorded, "person")

        self.assertEqual(preview["start"], 19.0)
        self.assertEqual(preview["end"], 94.8)
        self.assertEqual(preview["start_ts"], 99.0)
        self.assertEqual(preview["end_ts"], 174.8)

        recorded["end_off"] = 250.0
        capped = _detection_wall_preview(recorded, "person")
        self.assertEqual(capped["start"], 19.0)
        self.assertEqual(capped["end"], 109.0)
        self.assertEqual(capped["end_ts"], 189.0)

    def test_live_preview_follows_long_provisional_encounter(self) -> None:
        provisional = _event(
            20,
            100.0,
            "person",
            provisional=True,
            seg_path="front/segment.mp4",
            boxes=[{"cls": "person", "x1": 0.2, "y1": 0.2, "x2": 0.4, "y2": 0.5}],
        )
        provisional["start_off"] = 20.0
        provisional["end_off"] = 93.8

        preview = _detection_wall_preview(
            provisional,
            "person",
            {"start_ts": 50.0, "end_ts": 200.0},
        )

        self.assertEqual(preview["start_ts"], 99.0)
        self.assertEqual(preview["end_ts"], 174.8)

    def test_omits_preview_outside_live_window_or_for_unusable_event(self) -> None:
        provisional = _event(
            18,
            100.0,
            "person",
            provisional=True,
            seg_path="front/segment.mp4",
            boxes=[{"cls": "person", "x1": 0.2, "y1": 0.2, "x2": 0.4, "y2": 0.5}],
        )
        malformed = _event(
            19,
            100.0,
            "person",
            seg_path="front/segment.mp4",
            boxes=[{"cls": "person", "x1": 0.4, "y1": 0.2, "x2": 0.2, "y2": 0.5}],
        )

        self.assertIsNone(_detection_wall_preview(provisional, "person"))
        self.assertIsNone(_detection_wall_preview(
            provisional,
            "person",
            {"start_ts": 101.1, "end_ts": 110.0},
        ))
        self.assertIsNone(_detection_wall_preview(malformed, "person"))

    def test_image_api_paths_bypass_gzip(self) -> None:
        prefixes = (
            "/video/live/",
            "/api/thumb",
            "/api/video/event-thumb/",
            "/api/video/live-thumb",
        )
        suffixes = (".jpg", ".jpeg")

        self.assertTrue(_gzip_path_is_excluded(
            "/api/video/event-thumb/p%3a1%3aperson%3a2.0",
            prefixes,
            suffixes,
        ))
        self.assertTrue(_gzip_path_is_excluded(
            "/api/notifications/42/thumb",
            prefixes,
            suffixes,
        ))
        self.assertFalse(_gzip_path_is_excluded(
            "/api/detections/wall",
            prefixes,
            suffixes,
        ))


class DetectionWallDatabaseTests(unittest.TestCase):
    def test_provisional_cache_refreshes_when_a_detection_arrives(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = VideoSegmentDB(Path(tmpdir) / "video.db")
            media_start = time.time() - 10.0
            segment_id = db.open_segment(
                "front", "front/open.mp4", media_start
            )
            db.set_segment_media_start(segment_id, media_start)
            detection = {
                "has_human": True,
                "confidence": 0.9,
                "classes": ["person"],
            }
            db.insert_live_detections(segment_id, "front", [{
                **detection,
                "abs_ts": media_start + 1.0,
                "boxes": [_box(0.2, 0.4)],
            }])

            self.assertEqual(db.provisional_events("front"), [])

            db.insert_live_detections(segment_id, "front", [{
                **detection,
                "abs_ts": media_start + 1.5,
                "boxes": [_box(0.21, 0.4)],
            }])
            events = db.provisional_events("front")

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["class"], "person")

    def test_counts_only_request_skips_detection_page_work(self) -> None:
        class SourceDB:
            def to_source_configs(self):
                return (SourceConfig(id="front", name="Front"),)

            def get_setting(self, _key):
                return None

        class CountsDB:
            def __init__(self):
                self.count_calls = []

            def list_zones(self):
                return [{
                    "uid": "nearby",
                    "source_id": "front",
                    "name": "Nearby",
                    "polygon": _area(0.0, 0.0, 0.5, 0.5),
                    "enabled": True,
                }]

            def detection_class_counts(
                self, source_id, polygons, include_provisional
            ):
                self.count_calls.append(
                    (source_id, polygons, include_provisional)
                )
                return {"person": 3}

        with tempfile.TemporaryDirectory() as tmpdir:
            video_db = CountsDB()
            app = make_app(
                AppConfig(),
                source_db=SourceDB(),
                video_dir=Path(tmpdir) / "footage",
                video_db=video_db,
            )
            route = next(
                route
                for route in app.app.routes
                if getattr(route, "path", None) == "/api/detections/wall"
            )
            request = Request({
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/api/detections/wall",
                "raw_path": b"/api/detections/wall",
                "query_string": b"source=front&zones=nearby&events=0",
                "headers": [],
                "client": ("test", 1),
                "server": ("test", 80),
            })
            response = asyncio.run(route.endpoint(request))

        payload = json.loads(response.body)
        self.assertEqual(payload["classes"], {"person": 3})
        self.assertEqual(payload["cameras"], [])
        self.assertEqual(len(video_db.count_calls), 1)
        self.assertEqual(video_db.count_calls[0][0], "front")
        self.assertTrue(video_db.count_calls[0][1])

    def test_wall_query_uses_persisted_encounters_not_object_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = VideoSegmentDB(Path(tmpdir) / "video.db")
            segment_id = db.open_segment("front", "front/segment.mp4", 100.0)
            db.set_segment_media_start(segment_id, 100.0)
            db.close_segment(segment_id, 130.0, None, None)
            base = {
                "track_id": None,
                "segment_id": segment_id,
                "source_id": "front",
                "start_off": 1.0,
                "end_off": 3.0,
                "confidence": 0.9,
                "boxes_json": None,
            }
            db.insert_object_events([
                {
                    **base,
                    "abs_ts": 101.0,
                    "display_ts": 101.0,
                    "class": "person",
                    "event_type": "appeared",
                },
                {
                    **base,
                    "abs_ts": 110.0,
                    "display_ts": 110.0,
                    "class": "person",
                    "event_type": "disappeared",
                },
                {
                    **base,
                    "abs_ts": 105.0,
                    "display_ts": 105.0,
                    "class": "car",
                    "event_type": "appeared",
                },
            ])
            db.insert_events([
                {
                    **base,
                    "abs_ts": 101.0,
                    "class": "person",
                    "event_type": "detection",
                },
                {
                    **base,
                    "abs_ts": 105.0,
                    "class": "car",
                    "event_type": "detection",
                },
            ])

            rows = db.list_detection_events(
                "front", ["person", "car"], limit=10
            )

            self.assertEqual(
                [(row["class"], row["event_type"]) for row in rows],
                [("car", "detection"), ("person", "detection")],
            )
            self.assertTrue(all(str(row["id"]).isdigit() for row in rows))

    def test_provisional_wall_groups_nearby_people(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = VideoSegmentDB(Path(tmpdir) / "video.db")
            media_start = time.time() - 10.0
            segment_id = db.open_segment(
                "front", "front/open.mp4", media_start
            )
            db.set_segment_media_start(segment_id, media_start)
            for offset in (1.0, 1.5):
                db.insert_live_detections(segment_id, "front", [{
                    "abs_ts": media_start + offset,
                    "has_human": True,
                    "confidence": 0.9,
                    "boxes": [
                        _box(0.20 + offset * 0.01, 0.4),
                        _box(0.25 + offset * 0.01, 0.4),
                    ],
                    "classes": ["person"],
                }])

            events = db.provisional_detection_events("front")

        self.assertEqual(len(events), 1)
        self.assertTrue(str(events[0]["id"]).startswith("g:"))
        boxes = json.loads(events[0]["boxes_json"])
        self.assertEqual(boxes[0]["group_size"], 2)

    def test_before_cursor_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = VideoSegmentDB(Path(tmpdir) / "video.db")
            segment_id = db.open_segment("front", "front/segment.mp4", 100.0)
            db.set_segment_media_start(segment_id, 100.0)
            db.close_segment(segment_id, 130.0, None, None)
            db.insert_events([
                {
                    "segment_id": segment_id,
                    "source_id": "front",
                    "abs_ts": ts,
                    "class": "person",
                    "start_off": ts - 100.0,
                    "end_off": ts - 99.0,
                    "confidence": 0.8,
                    "boxes_json": None,
                }
                for ts in (101.0, 102.0, 103.0)
            ])

            rows = db.list_detection_events(
                "front", None, limit=10, before=103.0
            )

            self.assertEqual([row["abs_ts"] for row in rows], [102.0, 101.0])

    def test_detection_counts_respect_multiple_areas(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = VideoSegmentDB(Path(tmpdir) / "video.db")
            segment_id = db.open_segment("front", "front/segment.mp4", 100.0)
            db.set_segment_media_start(segment_id, 100.0)
            db.close_segment(segment_id, 130.0, None, None)
            events = [
                ("person", 101.0, _box(0.1, 0.1, "person")),
                ("bird", 102.0, _box(0.9, 0.9, "bird")),
                ("dog", 103.0, _box(0.5, 0.5, "dog")),
            ]
            db.insert_events([
                {
                    "segment_id": segment_id,
                    "source_id": "front",
                    "abs_ts": ts,
                    "class": cls,
                    "start_off": ts - 100.0,
                    "end_off": ts - 99.0,
                    "confidence": 0.8,
                    "boxes_json": json.dumps([box]),
                }
                for cls, ts, box in events
            ])

            counts = db.detection_class_counts(
                "front",
                [
                    _area(0.0, 0.0, 0.3, 0.3),
                    _area(0.7, 0.7, 1.0, 1.0),
                ],
                include_provisional=False,
            )

            self.assertEqual(counts, {"person": 1, "bird": 1})


class EncounterGroupingTests(unittest.TestCase):
    segment = {
        "id": 7,
        "source_id": "front",
        "media_epoch": 100.0,
        "start_ts": 100.0,
        "duration_sec": 60.0,
    }

    @staticmethod
    def detection(ts: float, boxes: list[dict]) -> dict:
        return {
            "abs_ts": ts,
            "ts_offset": ts - 100.0,
            "boxes": boxes,
        }

    def test_nearby_pair_is_one_encounter_but_far_group_is_separate(self) -> None:
        detections = []
        for ts, shift in ((101.0, 0.0), (101.5, 0.02)):
            detections.append(self.detection(ts, [
                _box(0.20 + shift, 0.40),
                _box(0.25 + shift, 0.41),
                _box(0.80 - shift, 0.45),
            ]))

        rows = _encounter_events_from_detections(self.segment, detections)

        self.assertEqual(len(rows), 2)
        group_sizes = sorted(
            json.loads(row["boxes_json"])[0]["group_size"]
            for row in rows
        )
        self.assertEqual(group_sizes, [1, 2])

    def test_short_detection_gap_reconnects_moving_group(self) -> None:
        detections = [
            self.detection(101.0, [_box(0.60, 0.45)]),
            self.detection(101.5, [_box(0.55, 0.45)]),
            self.detection(104.3, [_box(0.30, 0.45)]),
            self.detection(104.8, [_box(0.25, 0.45)]),
        ]

        rows = _encounter_events_from_detections(self.segment, detections)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["abs_ts"], 101.0)
        self.assertEqual(rows[0]["start_off"], 1.0)
        self.assertAlmostEqual(rows[0]["end_off"], 4.8)

    def test_distant_false_positive_does_not_extend_departed_walker(self) -> None:
        detections = [
            self.detection(101.0, [_box(0.12, 0.40)]),
            self.detection(101.5, [_box(0.07, 0.39)]),
            # The walker has left at the upper-left. A small static shape at
            # the bottom-left appears inside the temporal gap but is not the
            # same encounter.
            self.detection(107.0, [_box(0.01, 0.73)]),
            self.detection(107.5, [_box(0.01, 0.73)]),
        ]

        rows = _encounter_events_from_detections(self.segment, detections)

        self.assertEqual(len(rows), 2)
        self.assertEqual(
            sorted(round(row["end_off"] - row["start_off"], 1) for row in rows),
            [0.5, 0.5],
        )

    def test_encounter_matches_area_entered_after_first_observation(self) -> None:
        detections = [
            self.detection(101.0, [_box(0.10, 0.45)]),
            self.detection(101.5, [_box(0.20, 0.45)]),
            self.detection(102.0, [_box(0.30, 0.45)]),
        ]
        area = _area(0.28, 0.40, 0.34, 0.50)

        rows = _encounter_events_from_detections(self.segment, detections)
        boxes = json.loads(rows[0]["boxes_json"])

        self.assertTrue(any(box.get("_zone_sample") for box in boxes))
        self.assertEqual(_filter_with_polygons(rows, [area]), rows)

    def test_area_trail_is_merged_when_encounter_crosses_segments(self) -> None:
        first = json.dumps([_box(0.10, 0.45)])
        second = json.dumps([_box(0.30, 0.45)])
        merged = _merge_encounter_area_boxes(first, second)
        event = {"boxes_json": merged}

        self.assertEqual(
            _filter_with_polygons(
                [event],
                [_area(0.28, 0.40, 0.34, 0.50)],
            ),
            [event],
        )
        self.assertTrue(json.loads(merged)[1]["_zone_sample"])

    def test_exclusion_suppresses_only_events_wholly_inside_it(self) -> None:
        exclusion = _area(0.05, 0.35, 0.20, 0.55)
        hidden = {"boxes_json": json.dumps([_box(0.10, 0.45)])}
        entered_then_left = {
            "boxes_json": json.dumps([
                _box(0.10, 0.45),
                {**_box(0.30, 0.45), "_zone_sample": True},
            ])
        }

        self.assertEqual(
            _filter_with_zone_policy(
                [hidden, entered_then_left], [], [exclusion]
            ),
            [entered_then_left],
        )


class VideoZonePersistenceTests(unittest.TestCase):
    def test_single_zone_updates_do_not_rewrite_neighbouring_areas(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = VideoSegmentDB(Path(tmpdir) / "video.db")
            activity = db.save_zone("front", {
                "name": "Path",
                "type": "activity_area",
                "polygon": _area(0.1, 0.1, 0.4, 0.4),
            })
            exclusion = db.save_zone("front", {
                "name": "Toy unicorn",
                "type": "exclusion_area",
                "polygon": _area(0.6, 0.6, 0.8, 0.9),
            })

            updated = db.save_zone("front", {
                "name": "Front path",
                "type": "activity_area",
                "polygon": _area(0.12, 0.1, 0.42, 0.4),
            }, activity["uid"])
            zones = db.list_zones("front")

            self.assertEqual(updated["uid"], activity["uid"])
            self.assertEqual(len(zones), 2)
            self.assertEqual(
                next(z for z in zones if z["uid"] == exclusion["uid"])["name"],
                "Toy unicorn",
            )
            self.assertEqual(db.exclusion_areas("front"), [exclusion["polygon"]])
            self.assertTrue(db.delete_zone("front", exclusion["uid"]))
            self.assertFalse(db.delete_zone("front", exclusion["uid"]))


class DetectionWallAllCameraTests(unittest.TestCase):
    sources = [
        {"id": "front", "name": "Front door", "record_mode": "continuous"},
        {"id": "garden", "name": "Garden", "record_mode": "continuous"},
    ]

    def test_interleaves_cameras_in_global_time_order(self) -> None:
        db = FakeVideoDB([
            _event(1, 100.0, "person", source_id="front"),
            _event(2, 104.0, "dog", source_id="garden"),
            _event(3, 102.0, "car", source_id="front"),
            _event(4, 101.0, "bird", source_id="garden"),
        ])

        camera = _detection_wall_all(
            db, self.sources, [], limit=8, before=None
        )

        self.assertEqual(camera["id"], "all")
        self.assertEqual(
            [
                (event["source_id"], event["display_ts"])
                for event in camera["events"]
            ],
            [
                ("garden", 104.0),
                ("front", 102.0),
                ("garden", 101.0),
                ("front", 100.0),
            ],
        )
        self.assertEqual(camera["events"][0]["source_name"], "Garden")

    def test_global_cursor_pages_without_repeating_rows(self) -> None:
        db = FakeVideoDB([
            *[
                _event(index, 200.0 - index * 2, "person", source_id="front")
                for index in range(6)
            ],
            *[
                _event(100 + index, 199.0 - index * 2, "person", source_id="garden")
                for index in range(6)
            ],
        ])

        first = _detection_wall_all(
            db, self.sources, [], limit=5, before=None
        )
        second = _detection_wall_all(
            db, self.sources, [], limit=5, before=first["next_before"]
        )

        self.assertEqual(len(first["events"]), 5)
        self.assertEqual(len(second["events"]), 5)
        first_ids = {
            (event["source_id"], event["id"]) for event in first["events"]
        }
        second_ids = {
            (event["source_id"], event["id"]) for event in second["events"]
        }
        self.assertFalse(first_ids & second_ids)
        self.assertLess(
            second["events"][0]["display_ts"],
            first["events"][-1]["display_ts"],
        )

    def test_area_selection_is_independent_per_camera(self) -> None:
        db = FakeVideoDB([
            _event(
                1, 100.0, "person", source_id="front", boxes=[_box(0.1, 0.1)]
            ),
            _event(
                2, 101.0, "person", source_id="garden", boxes=[_box(0.1, 0.1)]
            ),
            _event(
                3, 102.0, "person", source_id="front", boxes=[_box(0.8, 0.8)]
            ),
        ])

        camera = _detection_wall_all(
            db,
            self.sources,
            [],
            limit=8,
            before=None,
            polygons_by_source={
                "front": [_area(0.0, 0.0, 0.3, 0.3)],
            },
        )

        self.assertEqual(
            [(event["source_id"], event["id"]) for event in camera["events"]],
            [("garden", "2"), ("front", "1")],
        )


if __name__ == "__main__":
    unittest.main()
