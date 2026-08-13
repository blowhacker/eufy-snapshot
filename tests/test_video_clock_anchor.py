from __future__ import annotations

import sys
import json
import sqlite3
import tempfile
import time
import types
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard import media_time, sei
from wanyard.video import (
    VideoSegmentDB,
    VideoWorker,
    _CLOCK_ZERO_FIRST_FRAME,
    _decode_media_epoch,
)


class _FakeSEI(bytes):
    """Frame side-data entry carrying our unregistered-SEI clock payload.

    cv2 dropped side data; every clock consumer now reads it via PyAV, so the
    fakes attach a real SEI body that ``sei.decode_frame`` parses.
    """

    def __new__(cls, unix_seconds: float) -> "_FakeSEI":
        obj = super().__new__(cls, sei.build_payload(sei.encode_value(unix_seconds)))
        obj.type = types.SimpleNamespace(name="SEI_UNREGISTERED")
        return obj


class _FakeFrame:
    def __init__(
        self,
        frame_bgr: np.ndarray,
        *,
        pts: int | None = None,
        time_base: Fraction | None = None,
        side_data: list | None = None,
    ) -> None:
        self.frame_bgr = frame_bgr
        self.pts = pts
        self.time_base = time_base
        self.side_data = side_data or []

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
    def __init__(self, frames: list[_FakeFrame], *, has_video: bool = True) -> None:
        self.streams = []
        if has_video:
            self.streams.append(types.SimpleNamespace(type="video"))
        self._packets = [_FakePacket(frames)]
        self._frames = frames
        self.closed = False

    def demux(self, _stream):
        return list(self._packets)

    def decode(self, video=0):
        return list(self._frames)

    def close(self) -> None:
        self.closed = True


def _blank(width: int = 320) -> np.ndarray:
    return np.full((64, width, 3), 180, dtype=np.uint8)


class MediaEpochAnchorTests(unittest.TestCase):
    def test_decode_media_epoch_reads_first_video_frame(self) -> None:
        unix_seconds = 1_781_600_000.12
        frames = [
            _FakeFrame(_blank(), side_data=[_FakeSEI(unix_seconds)]),
            _FakeFrame(np.full_like(_blank(), 40),
                       side_data=[_FakeSEI(unix_seconds + 10.0)]),
        ]
        container = _FakeContainer(frames)

        fake_av = types.SimpleNamespace(open=lambda _path: container)
        with mock.patch.dict(sys.modules, {"av": fake_av}):
            decoded = _decode_media_epoch("segment.mp4")

        self.assertIsNotNone(decoded)
        self.assertEqual(round(decoded * 100), sei.encode_value(unix_seconds))
        self.assertTrue(container.closed)

    def test_decode_media_epoch_returns_none_without_clock(self) -> None:
        container = _FakeContainer([_FakeFrame(_blank(), side_data=[])])

        fake_av = types.SimpleNamespace(open=lambda _path: container)
        with mock.patch.dict(sys.modules, {"av": fake_av}):
            decoded = _decode_media_epoch("segment.mp4")

        self.assertIsNone(decoded)
        self.assertTrue(container.closed)

    def test_decode_media_epoch_returns_none_without_video(self) -> None:
        container = _FakeContainer([], has_video=False)

        fake_av = types.SimpleNamespace(open=lambda _path: container)
        with mock.patch.dict(sys.modules, {"av": fake_av}):
            decoded = _decode_media_epoch("audio-only.mp4")

        self.assertIsNone(decoded)
        self.assertTrue(container.closed)

    def test_decode_media_epoch_uses_mp4_player_offset(self) -> None:
        unix_seconds = 1_781_600_010.12
        frames = [
            _FakeFrame(_blank(), pts=100, time_base=Fraction(1, 1000)),
            _FakeFrame(_blank(), pts=1100, time_base=Fraction(1, 1000)),
            _FakeFrame(_blank(), pts=2100, time_base=Fraction(1, 1000),
                       side_data=[_FakeSEI(unix_seconds + 2.0)]),
        ]
        container = _FakeContainer(frames)

        fake_av = types.SimpleNamespace(open=lambda _path: container)
        with mock.patch.dict(sys.modules, {"av": fake_av}):
            decoded = _decode_media_epoch("segment.mp4")

        self.assertIsNotNone(decoded)
        self.assertEqual(round(decoded * 100), sei.encode_value(unix_seconds - 0.1))
        self.assertTrue(container.closed)

    def test_decode_media_epoch_can_use_live_provisional_zero(self) -> None:
        unix_seconds = 1_781_600_010.12
        frames = [
            _FakeFrame(_blank(), pts=100, time_base=Fraction(1, 1000)),
            _FakeFrame(_blank(), pts=1100, time_base=Fraction(1, 1000)),
            _FakeFrame(_blank(), pts=2100, time_base=Fraction(1, 1000),
                       side_data=[_FakeSEI(unix_seconds + 2.0)]),
        ]
        container = _FakeContainer(frames)

        fake_av = types.SimpleNamespace(open=lambda _path: container)
        with mock.patch.dict(sys.modules, {"av": fake_av}):
            decoded = _decode_media_epoch(
                "segment.ts",
                playback_zero=_CLOCK_ZERO_FIRST_FRAME,
            )

        self.assertIsNotNone(decoded)
        self.assertEqual(round(decoded * 100), sei.encode_value(unix_seconds))
        self.assertTrue(container.closed)

    def test_live_hls_window_uses_decoded_clock_not_pdt(self) -> None:
        marker_ts = 1_781_700_010.12
        container = _FakeContainer([
            _FakeFrame(_blank(), pts=100, time_base=Fraction(1, 1000)),
            _FakeFrame(_blank(), pts=600, time_base=Fraction(1, 1000),
                       side_data=[_FakeSEI(marker_ts)]),
        ])
        opened: list[str] = []

        def fake_open(path: str):
            opened.append(Path(path).name)
            return container

        with tempfile.TemporaryDirectory(prefix="wanyard-live-window-") as tmp:
            live_dir = Path(tmp) / "live" / "front"
            live_dir.mkdir(parents=True)
            (live_dir / "seg_a.ts").write_bytes(b"a")
            (live_dir / "seg_b.ts").write_bytes(b"b")
            (live_dir / "live.m3u8").write_text(
                "#EXTM3U\n"
                "#EXT-X-TARGETDURATION:4\n"
                "#EXT-X-PROGRAM-DATE-TIME:2020-01-01T00:00:00Z\n"
                "#EXTINF:4.0,\n"
                "seg_a.ts\n"
                "#EXT-X-PROGRAM-DATE-TIME:2020-01-01T00:00:04Z\n"
                "#EXTINF:4.0,\n"
                "seg_b.ts\n",
                encoding="utf-8",
            )

            fake_av = types.SimpleNamespace(open=fake_open)
            with mock.patch.dict(sys.modules, {"av": fake_av}):
                window = media_time.live_window(Path(tmp), "front")

        assert window is not None
        self.assertEqual(opened, ["seg_b.ts"])
        self.assertAlmostEqual(window["segments"][1]["start_ts"], marker_ts - 0.5, places=2)
        self.assertAlmostEqual(window["start_ts"], marker_ts - 4.5, places=2)
        self.assertGreater(abs(window["start_ts"] - 1_577_836_800.0), 1_000_000.0)
        self.assertTrue(container.closed)

    def test_hls_frame_read_matches_nearest_clock(self) -> None:
        target = 1_781_700_100.0
        early = np.full((64, 320, 3), 50, dtype=np.uint8)
        close = np.full((64, 320, 3), 150, dtype=np.uint8)
        late = np.full((64, 320, 3), 250, dtype=np.uint8)
        container = _FakeContainer([
            _FakeFrame(early, side_data=[_FakeSEI(target - 0.4)]),
            _FakeFrame(close, side_data=[_FakeSEI(target + 0.03)]),
            _FakeFrame(late, side_data=[_FakeSEI(target + 1.0)]),
        ])
        fake_av = types.SimpleNamespace(open=lambda _path: container)

        with mock.patch.dict(sys.modules, {"av": fake_av}):
            frame = media_time._decode_ts_frame_at_sei(
                Path("seg.ts"), target, max_drift=0.2)

        self.assertIsNotNone(frame)
        self.assertEqual(int(frame[0, 0, 0]), 150)

    def test_live_resolver_prefers_exact_chunk_over_earlier_edge_slop(self) -> None:
        chunks = [
            {"uri": "earlier.ts", "start_ts": 98.0, "end_ts": 100.0,
             "duration": 2.0, "media_offset": 0.0},
            {"uri": "exact.ts", "start_ts": 100.0, "end_ts": 102.0,
             "duration": 2.0, "media_offset": 2.0},
        ]

        with mock.patch.object(media_time, "_live_chunks", return_value=chunks):
            location = media_time._resolve_live(Path("video"), "front", 101.0)

        assert location is not None
        self.assertEqual(location.anchor.asset_ref, "exact.ts")


class VideoDbLogicTests(unittest.TestCase):
    def test_close_segment_accepts_authoritative_video_duration(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wanyard-duration-") as tmp:
            db = VideoSegmentDB(Path(tmp) / "video.sqlite")
            segment_id = db.open_segment("front", "front/segment.mp4", 100.0)
            db.close_segment(
                segment_id, 125.0, None, None, playable_duration=9.25
            )

            segment = db.get_segment(segment_id)

        assert segment is not None
        self.assertEqual(segment["end_ts"], 125.0)
        self.assertEqual(segment["duration_sec"], 9.25)

    def test_recent_duration_repair_shrinks_only_suspicious_rows(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wanyard-duration-repair-") as tmp:
            root = Path(tmp)
            db = VideoSegmentDB(root / "video.sqlite")
            segment_id = db.open_segment(
                "front", "front/segment.mp4", time.time() - 60
            )
            db.set_segment_media_start(segment_id, time.time() - 60)
            db.close_segment(segment_id, time.time(), None, None)
            media = root / "front/segment.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"video")
            media.with_name(media.name + ".clock.json").write_text(
                json.dumps({"version": 1, "frames": [[0.0, 1], [8.9, 2]]})
            )
            worker = VideoWorker(types.SimpleNamespace(id="front"), root, db)

            with mock.patch(
                "wanyard.video._video_stream_duration", return_value=9.2
            ):
                worker._repair_recent_segment_durations()

            segment = db.get_segment(segment_id)

        assert segment is not None
        self.assertEqual(segment["duration_sec"], 9.2)

    def test_whole_frame_class_counts_skip_exclusion_geometry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wanyard-class-counts-") as tmp:
            db = VideoSegmentDB(Path(tmp) / "video.sqlite")
            segment_id = db.open_segment("front", "front/segment.mp4", 100.0)
            db.set_segment_media_start(segment_id, 100.0)
            db.close_segment(segment_id, 130.0, None, None)

            def box(cx: float, cy: float) -> dict:
                return {
                    "cls": "person", "x1": cx - 0.02, "y1": cy - 0.02,
                    "x2": cx + 0.02, "y2": cy + 0.02,
                }

            db.insert_events([
                {
                    "segment_id": segment_id, "source_id": "front",
                    "abs_ts": 101.0 + index, "class": "person",
                    "start_off": 1.0 + index, "end_off": 2.0 + index,
                    "confidence": 0.8, "boxes_json": json.dumps([event_box]),
                }
                for index, event_box in enumerate((box(0.1, 0.1), box(0.8, 0.8)))
            ])
            db.save_zone("front", {
                "name": "Ignore corner", "type": "exclusion_area",
                "polygon": [
                    {"x": 0.0, "y": 0.0}, {"x": 0.3, "y": 0.0},
                    {"x": 0.3, "y": 0.3}, {"x": 0.0, "y": 0.3},
                ],
            })

            whole_frame = db.class_counts(
                "front", include_provisional=False, zone_id="none"
            )
            exclusion_filtered = db.class_counts(
                "front", include_provisional=False
            )

        self.assertEqual(whole_frame, {"person": 2})
        self.assertEqual(exclusion_filtered, {"person": 1})

    def test_live_status_returns_requested_detection_window(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wanyard-live-status-") as tmp:
            db = VideoSegmentDB(Path(tmp) / "video.sqlite")
            base = time.time() - 10.0
            segment_id = db.open_segment("front", "front/live.mp4", base)
            db.set_segment_media_start(segment_id, base)
            db.insert_live_detections(segment_id, "front", [
                {
                    "abs_ts": base + 10.0,
                    "has_human": True,
                    "confidence": 0.8,
                    "boxes": [{"cls": "person", "x1": 0.1, "y1": 0.1, "x2": 0.2, "y2": 0.2}],
                    "classes": ["person"],
                },
                {
                    "abs_ts": base + 20.0,
                    "has_human": True,
                    "confidence": 0.9,
                    "boxes": [{"cls": "person", "x1": 0.3, "y1": 0.3, "x2": 0.4, "y2": 0.4}],
                    "classes": ["person"],
                },
            ])

            status = db.live_status("front", det_since=base + 19.0, det_until=base + 21.0)

        self.assertEqual(
            [d["abs_ts"] for d in status["recent_detections"]],
            [base + 20.0],
        )

    def test_provisional_event_id_resolves_for_thumbnail_crop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wanyard-provisional-event-") as tmp:
            db = VideoSegmentDB(Path(tmp) / "video.sqlite")
            base = time.time() - 10.0
            segment_id = db.open_segment("front", "front/live.mp4", base)
            db.set_segment_media_start(segment_id, base)
            box = {
                "cls": "person",
                "conf": 0.8,
                "x1": 0.1,
                "y1": 0.2,
                "x2": 0.3,
                "y2": 0.5,
            }
            db.insert_live_detections(segment_id, "front", [
                {
                    "abs_ts": base + 2.0,
                    "has_human": True,
                    "confidence": 0.8,
                    "boxes": [box],
                    "classes": ["person"],
                },
                {
                    "abs_ts": base + 2.5,
                    "has_human": True,
                    "confidence": 0.82,
                    "boxes": [{**box, "conf": 0.82}],
                    "classes": ["person"],
                },
            ])

            events = db.provisional_events("front")
            event = next(e for e in events if e["class"] == "person")
            resolved = db.get_event_with_segment(event["id"])

        assert resolved is not None
        self.assertEqual(resolved["id"], event["id"])
        self.assertEqual(resolved["seg_path"], "front/live.mp4")
        self.assertEqual(resolved["seg_media_epoch"], base)
        self.assertEqual(resolved["seg_end_ts"], None)
        self.assertEqual(round(resolved["abs_ts"], 1), round(base + 2.5, 1))
        boxes = json.loads(resolved["boxes_json"])
        self.assertEqual(boxes[0]["cls"], "person")
        self.assertEqual(boxes[0]["conf"], 0.82)

    def test_notification_class_recovers_for_detection_refs(self) -> None:
        """d:<id> refs resolve with NULL class; the notification's own class
        must drive box selection (a frame can hold a high-conf car AND the
        notified bird — classless selection crops the car)."""
        with tempfile.TemporaryDirectory(prefix="wanyard-video-db-") as tmp:
            db = VideoSegmentDB(Path(tmp) / "video.sqlite")
            with db._connect() as conn:
                rule_id = conn.execute(
                    "INSERT INTO notification_rules(name, source_id)"
                    " VALUES('Bird','garden')").lastrowid
                conn.execute(
                    "INSERT INTO notification_events"
                    " (rule_id, rule_name, source_id, zone_ref, event_ref,"
                    "  event_ts, class, title, body)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (rule_id, "Bird", "garden", "whole_frame", "d:123",
                     1_781_600_010.0, "bird", "t", "b"),
                )
            self.assertEqual(db.notification_class_for_ref("d:123"), "bird")
            self.assertIsNone(db.notification_class_for_ref("d:999"))

    def test_old_object_event_thumbnail_uses_frame_that_supplied_box(self) -> None:
        base = 1_781_600_000.0
        with tempfile.TemporaryDirectory(prefix="wanyard-video-db-") as tmp:
            db = VideoSegmentDB(Path(tmp) / "video.sqlite")
            segment_id = db.open_segment("desk", "desk/segment.mp4", base)
            db.set_segment_media_start(segment_id, base)
            db.close_segment(segment_id, base + 60.0, None, None)
            first_box = {
                "cls": "person", "conf": 0.45, "track_id": "desk:test:72",
                "x1": 0.58, "y1": 0.70, "x2": 0.63, "y2": 0.78,
            }
            representative_box = {
                "cls": "person", "conf": 0.71, "track_id": "desk:test:72",
                "x1": 0.30, "y1": 0.70, "x2": 0.33, "y2": 0.77,
            }
            db.insert_live_detections(segment_id, "desk", [
                {
                    "abs_ts": base + 39.67,
                    "has_human": True,
                    "confidence": 0.45,
                    "boxes": [first_box],
                    "classes": ["person"],
                },
                {
                    "abs_ts": base + 45.0,
                    "has_human": True,
                    "confidence": 0.71,
                    "boxes": [representative_box],
                    "classes": ["person"],
                },
            ])
            # Legacy construction: first observation's time, later/best box.
            db.insert_object_events([{
                "track_id": None,
                "segment_id": segment_id,
                "source_id": "desk",
                "abs_ts": base + 39.67,
                "display_ts": base + 39.67,
                "class": "person",
                "event_type": "appeared",
                "start_off": 39.67,
                "end_off": 45.0,
                "confidence": 0.71,
                "boxes_json": json.dumps([representative_box]),
            }])
            with db._connect() as conn:
                event_id = conn.execute(
                    "SELECT id FROM object_events"
                ).fetchone()["id"]

            resolved = db.get_event_with_segment(f"o:{event_id}")

        assert resolved is not None
        self.assertAlmostEqual(resolved["abs_ts"], base + 39.67)
        self.assertAlmostEqual(resolved["thumbnail_abs_ts"], base + 45.0)

    def test_dead_notification_ref_re_resolves_to_nearest_detection(self) -> None:
        """Backfill's replace_detections churns detection ids; a notification's
        d:<id> ref must re-resolve via (source, time) to an equivalent row."""
        base = 1_781_600_000.0
        with tempfile.TemporaryDirectory(prefix="wanyard-video-db-") as tmp:
            db = VideoSegmentDB(Path(tmp) / "video.sqlite")
            seg_id = db.open_segment("front", "front/seg.mp4", base)
            db.set_segment_media_start(seg_id, base)
            db.close_segment(seg_id, base + 60, None, None)
            # the replacement detection: same instant, new id
            db.insert_live_detections(seg_id, "front", [{
                "abs_ts": base + 10.4, "has_human": True, "confidence": 0.9,
                "boxes": [{"x1": 0.1, "y1": 0.1, "x2": 0.4, "y2": 0.6,
                           "conf": 0.9, "cls": "person"}],
                "classes": ["person"],
            }])
            with db._connect() as conn:
                rule_id = conn.execute(
                    "INSERT INTO notification_rules(name, source_id)"
                    " VALUES('r','front')").lastrowid
                conn.execute(
                    "INSERT INTO notification_events"
                    " (rule_id, rule_name, source_id, zone_ref, event_ref,"
                    "  event_ts, class, title, body)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (rule_id, "r", "front", "whole_frame", "d:999999",
                     base + 10.0, "person", "t", "b"),
                )

            # the referenced id 999999 does not exist — re-resolution kicks in
            self.assertIsNone(db.get_event_with_segment("d:999999"))
            evt = db.event_like_for_notification_ref("d:999999")
            self.assertIsNotNone(evt)
            self.assertEqual(evt["seg_path"], "front/seg.mp4")
            self.assertAlmostEqual(evt["abs_ts"], base + 10.4, delta=1e-6)
            self.assertEqual(evt["seg_media_epoch"], base)

            # outside tolerance -> no stand-in
            self.assertIsNone(
                db.event_like_for_notification_ref("d:999999", tolerance=0.2))
            # unknown ref -> None
            self.assertIsNone(db.event_like_for_notification_ref("d:1"))

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


class DirectionalGapResolveTests(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE segments(
                id INTEGER PRIMARY KEY,
                source_id TEXT NOT NULL,
                path TEXT NOT NULL,
                start_ts REAL NOT NULL,
                end_ts REAL,
                media_epoch REAL,
                duration_sec REAL
            );
            INSERT INTO segments VALUES
                (1, 'garden', 'garden/one.mp4', 100, 120, 100, 20),
                (3, 'garden', 'garden/unanchored.mp4', 121, 179, NULL, 58),
                (2, 'garden', 'garden/two.mp4', 180, 200, 180, 20);
        """)
        return conn

    def test_backward_navigation_lands_at_previous_recording_edge(self) -> None:
        location = media_time.resolve(
            self._conn(), Path("/missing"), "garden", 170, direction="backward"
        )

        self.assertEqual(location.provider, "mp4")
        self.assertEqual(location.segment_id, 1)
        self.assertEqual(location.reason, "gap-backward")
        self.assertEqual(location.media_offset, 20)

    def test_forward_navigation_lands_at_next_recording_edge(self) -> None:
        location = media_time.resolve(
            self._conn(), Path("/missing"), "garden", 130, direction="forward"
        )

        self.assertEqual(location.provider, "mp4")
        self.assertEqual(location.segment_id, 2)
        self.assertEqual(location.reason, "gap-forward")
        self.assertEqual(location.media_offset, 0)

    def test_plain_timestamp_resolution_does_not_hide_a_gap(self) -> None:
        location = media_time.resolve(
            self._conn(), Path("/missing"), "garden", 120.75
        )

        self.assertEqual(location.provider, "none")
        self.assertEqual(location.reason, "gap")

    def test_directional_navigation_skips_an_unanchored_file(self) -> None:
        location = media_time.resolve(
            self._conn(), Path("/missing"), "garden", 150, direction="backward"
        )

        self.assertEqual(location.provider, "mp4")
        self.assertEqual(location.segment_id, 1)
        self.assertEqual(location.reason, "gap-backward")

    def test_plain_timestamp_surfaces_an_unanchored_file(self) -> None:
        location = media_time.resolve(
            self._conn(), Path("/missing"), "garden", 150
        )

        self.assertEqual(location.provider, "none")
        self.assertEqual(location.reason, "no_anchor")


class RoundTripInvariantTests(unittest.TestCase):
    """check_detection_round_trip: resolve(media->clock(detection)) must land on
    a usable asset whose clock time equals the detection's, within EPS. The
    invariant is on clock time, not offset (a boundary-duplicate instant
    resolves to the adjacent segment at a different offset and is still correct)."""

    def _conn(self, media_epoch: float, abs_ts: float) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE segments(id INTEGER PRIMARY KEY, media_epoch REAL);"
            "CREATE TABLE video_detections("
            "id INTEGER PRIMARY KEY, source_id TEXT, abs_ts REAL, segment_id INTEGER);"
        )
        conn.execute("INSERT INTO segments(id, media_epoch) VALUES(1,?)", (media_epoch,))
        conn.execute(
            "INSERT INTO video_detections(id, source_id, abs_ts, segment_id)"
            " VALUES(1,'cam',?,1)", (abs_ts,))
        return conn

    @staticmethod
    def _loc(epoch: float, media_offset: float):
        return types.SimpleNamespace(
            provider="mp4",
            anchor=types.SimpleNamespace(media_to_clock=lambda off: epoch + off),
            media_offset=media_offset,
            reason="ok",
        )

    def test_ok_when_resolved_clock_matches(self) -> None:
        conn = self._conn(media_epoch=1000.0, abs_ts=1005.0)   # expected offset 5.0
        with mock.patch.object(media_time, "resolve", return_value=self._loc(1000.0, 5.0)):
            rt = media_time.check_detection_round_trip(conn, Path("/x"), 1)
        self.assertTrue(rt.ok)
        self.assertEqual(rt.status, "ok")
        self.assertFalse(rt.alternate)
        self.assertLess(rt.world_delta, media_time.EPS)

    def test_alternate_segment_same_instant_is_ok(self) -> None:
        # boundary duplicate: adjacent segment (epoch 1002, offset 3) -> same clock
        conn = self._conn(media_epoch=1000.0, abs_ts=1005.0)   # expected offset 5.0
        with mock.patch.object(media_time, "resolve", return_value=self._loc(1002.0, 3.0)):
            rt = media_time.check_detection_round_trip(conn, Path("/x"), 1)
        self.assertTrue(rt.ok)
        self.assertTrue(rt.alternate)                          # offset differs, clock matches

    def test_world_mismatch_when_resolved_clock_drifts(self) -> None:
        conn = self._conn(media_epoch=1000.0, abs_ts=1005.0)
        with mock.patch.object(media_time, "resolve", return_value=self._loc(1000.0, 9.0)):
            rt = media_time.check_detection_round_trip(conn, Path("/x"), 1)
        self.assertFalse(rt.ok)
        self.assertEqual(rt.status, "world_mismatch")

    def test_no_detection_for_missing_row(self) -> None:
        conn = self._conn(media_epoch=1000.0, abs_ts=1005.0)
        rt = media_time.check_detection_round_trip(conn, Path("/x"), 999)
        self.assertFalse(rt.ok)
        self.assertEqual(rt.status, "no_detection")

    def test_propagates_resolve_failure(self) -> None:
        conn = self._conn(media_epoch=1000.0, abs_ts=1005.0)
        miss = types.SimpleNamespace(provider="none", anchor=None,
                                     media_offset=None, reason="no_anchor")
        with mock.patch.object(media_time, "resolve", return_value=miss):
            rt = media_time.check_detection_round_trip(conn, Path("/x"), 1)
        self.assertFalse(rt.ok)
        self.assertEqual(rt.status, "no_anchor")


if __name__ == "__main__":
    unittest.main()
