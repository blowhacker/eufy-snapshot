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

from wanyard import bitc, media_time
from wanyard.video import (
    VideoSegmentDB,
    _BITC_ZERO_FIRST_FRAME,
    _decode_bitc_media_epoch,
    _yolo_tag_video,
)


class _FakeFrame:
    def __init__(
        self,
        frame_bgr: np.ndarray,
        *,
        pts: int | None = None,
        time_base: Fraction | None = None,
    ) -> None:
        self.frame_bgr = frame_bgr
        self.pts = pts
        self.time_base = time_base

    def to_ndarray(self, *, format: str):
        if format != "bgr24":
            raise AssertionError(format)
        return self.frame_bgr.copy()


class _FakePacket:
    def __init__(self, frames: list[np.ndarray] | list[_FakeFrame]) -> None:
        self._frames = [
            frame if isinstance(frame, _FakeFrame) else _FakeFrame(frame)
            for frame in frames
        ]

    def decode(self):
        return list(self._frames)


class _FakeContainer:
    def __init__(
        self,
        frames: list[np.ndarray] | list[_FakeFrame],
        *,
        has_video: bool = True,
    ) -> None:
        self.streams = []
        if has_video:
            self.streams.append(types.SimpleNamespace(type="video"))
        self._packets = [_FakePacket(frames)]
        self.closed = False

    def demux(self, _stream):
        return list(self._packets)

    def close(self) -> None:
        self.closed = True


class _FakeCapture:
    def __init__(self, frames: list[np.ndarray], offsets: list[float], fps: float) -> None:
        self.frames = frames
        self.offsets = offsets
        self.fps = fps
        self.index = -1
        self.released = False

    def isOpened(self) -> bool:
        return True

    def read(self):
        self.index += 1
        if self.index >= len(self.frames):
            return False, None
        return True, self.frames[self.index].copy()

    def get(self, prop):
        if prop == 5:
            return self.fps
        if prop == 0:
            return self.offsets[self.index] * 1000.0
        raise AssertionError(prop)

    def release(self) -> None:
        self.released = True


class _FakeModel:
    def __init__(self) -> None:
        self.frames: list[np.ndarray] = []

    def predict(self, frame, **_kwargs):
        self.frames.append(frame.copy())
        return ["unused"]


class _FakeVideoDB:
    def __init__(self, media_epoch: float) -> None:
        self.segment = {"media_epoch": media_epoch}
        self.detections: list[dict] | None = None
        self.scanned: list[int] = []

    def get_segment(self, _segment_id: int):
        return self.segment

    def replace_detections(self, _segment_id: int, detections: list[dict]) -> None:
        self.detections = detections

    def mark_scanned(self, segment_id: int) -> None:
        self.scanned.append(segment_id)


class VideoBitcAnchorTests(unittest.TestCase):
    def test_decode_bitc_media_epoch_reads_first_video_frame(self) -> None:
        unix_seconds = 1_781_600_000.12
        first = np.full((64, bitc.WIDTH + 16, 3), 180, dtype=np.uint8)
        second = np.full_like(first, 40)
        bitc.render(first, bitc.encode_value(unix_seconds))
        bitc.render(second, bitc.encode_value(unix_seconds + 10.0))
        container = _FakeContainer([first, second])

        fake_av = types.SimpleNamespace(open=lambda _path: container)
        with mock.patch.dict(sys.modules, {"av": fake_av}):
            decoded = _decode_bitc_media_epoch("segment.mp4")

        self.assertIsNotNone(decoded)
        self.assertEqual(round(decoded * 100), bitc.encode_value(unix_seconds))
        self.assertTrue(container.closed)

    def test_decode_bitc_media_epoch_returns_none_without_valid_marker(self) -> None:
        frame = np.full((64, bitc.WIDTH + 16, 3), 180, dtype=np.uint8)
        container = _FakeContainer([frame])

        fake_av = types.SimpleNamespace(open=lambda _path: container)
        with mock.patch.dict(sys.modules, {"av": fake_av}):
            decoded = _decode_bitc_media_epoch("segment.mp4")

        self.assertIsNone(decoded)
        self.assertTrue(container.closed)

    def test_decode_bitc_media_epoch_returns_none_without_video(self) -> None:
        container = _FakeContainer([], has_video=False)

        fake_av = types.SimpleNamespace(open=lambda _path: container)
        with mock.patch.dict(sys.modules, {"av": fake_av}):
            decoded = _decode_bitc_media_epoch("audio-only.mp4")

        self.assertIsNone(decoded)
        self.assertTrue(container.closed)

    def test_decode_bitc_media_epoch_uses_mp4_player_offset(self) -> None:
        unix_seconds = 1_781_600_010.12
        blank = np.full((64, bitc.WIDTH + 16, 3), 180, dtype=np.uint8)
        marked = blank.copy()
        bitc.render(marked, bitc.encode_value(unix_seconds + 2.0))
        frames = [
            _FakeFrame(blank, pts=100, time_base=Fraction(1, 1000)),
            _FakeFrame(blank, pts=1100, time_base=Fraction(1, 1000)),
            _FakeFrame(marked, pts=2100, time_base=Fraction(1, 1000)),
        ]
        container = _FakeContainer(frames)

        fake_av = types.SimpleNamespace(open=lambda _path: container)
        with mock.patch.dict(sys.modules, {"av": fake_av}):
            decoded = _decode_bitc_media_epoch("segment.mp4")

        self.assertIsNotNone(decoded)
        self.assertEqual(round(decoded * 100), bitc.encode_value(unix_seconds - 0.1))
        self.assertTrue(container.closed)

    def test_decode_bitc_media_epoch_can_use_live_provisional_zero(self) -> None:
        unix_seconds = 1_781_600_010.12
        blank = np.full((64, bitc.WIDTH + 16, 3), 180, dtype=np.uint8)
        marked = blank.copy()
        bitc.render(marked, bitc.encode_value(unix_seconds + 2.0))
        frames = [
            _FakeFrame(blank, pts=100, time_base=Fraction(1, 1000)),
            _FakeFrame(blank, pts=1100, time_base=Fraction(1, 1000)),
            _FakeFrame(marked, pts=2100, time_base=Fraction(1, 1000)),
        ]
        container = _FakeContainer(frames)

        fake_av = types.SimpleNamespace(open=lambda _path: container)
        with mock.patch.dict(sys.modules, {"av": fake_av}):
            decoded = _decode_bitc_media_epoch(
                "segment.ts",
                playback_zero=_BITC_ZERO_FIRST_FRAME,
            )

        self.assertIsNotNone(decoded)
        self.assertEqual(round(decoded * 100), bitc.encode_value(unix_seconds))
        self.assertTrue(container.closed)

    def test_live_hls_window_uses_decoded_bitc_not_pdt(self) -> None:
        marker_ts = 1_781_700_010.12
        blank = np.full((64, bitc.WIDTH + 16, 3), 180, dtype=np.uint8)
        marked = blank.copy()
        bitc.render(marked, bitc.encode_value(marker_ts))
        container = _FakeContainer([
            _FakeFrame(blank, pts=100, time_base=Fraction(1, 1000)),
            _FakeFrame(marked, pts=600, time_base=Fraction(1, 1000)),
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

    def test_live_hls_window_uses_sei_before_pixel_fallback(self) -> None:
        marker_ts = 1_781_700_010.12
        blank = np.full((64, bitc.WIDTH + 16, 3), 180, dtype=np.uint8)
        container = _FakeContainer([
            _FakeFrame(blank, pts=100, time_base=Fraction(1, 1000)),
            _FakeFrame(blank, pts=600, time_base=Fraction(1, 1000)),
        ])

        with tempfile.TemporaryDirectory(prefix="wanyard-live-window-sei-") as tmp:
            live_dir = Path(tmp) / "live" / "front"
            live_dir.mkdir(parents=True)
            (live_dir / "seg.ts").write_bytes(b"sei")
            (live_dir / "live.m3u8").write_text(
                "#EXTM3U\n"
                "#EXT-X-TARGETDURATION:4\n"
                "#EXTINF:4.0,\n"
                "seg.ts\n",
                encoding="utf-8",
            )

            fake_av = types.SimpleNamespace(open=lambda _path: container)
            with (
                mock.patch.dict(sys.modules, {"av": fake_av}),
                mock.patch(
                    "wanyard.sei.decode_frame",
                    side_effect=[(None, False), (marker_ts, True)],
                ) as decode_sei,
                mock.patch("wanyard.bitc.decode", return_value=(None, False)) as decode_pixel,
            ):
                window = media_time.live_window(Path(tmp), "front")

        assert window is not None
        self.assertEqual(decode_sei.call_count, 2)
        self.assertEqual(decode_pixel.call_count, 1)
        self.assertAlmostEqual(window["start_ts"], marker_ts - 0.5, places=2)
        self.assertTrue(container.closed)

    def test_hls_frame_read_matches_nearest_decoded_bitc(self) -> None:
        target = 1_781_700_100.0
        early = np.full((64, bitc.WIDTH + 16, 3), 50, dtype=np.uint8)
        close = np.full((64, bitc.WIDTH + 16, 3), 150, dtype=np.uint8)
        late = np.full((64, bitc.WIDTH + 16, 3), 250, dtype=np.uint8)
        bitc.render(early, bitc.encode_value(target - 0.4))
        bitc.render(close, bitc.encode_value(target + 0.03))
        bitc.render(late, bitc.encode_value(target + 1.0))
        capture = _FakeCapture([early, close, late], [0.0, 0.1, 0.2], 10.0)
        fake_cv2 = types.SimpleNamespace(VideoCapture=lambda _path: capture)

        with mock.patch.dict(sys.modules, {"cv2": fake_cv2}):
            frame = media_time._decode_ts_frame_at_bitc(
                Path("seg.ts"), target, max_drift=0.2)

        self.assertIsNotNone(frame)
        self.assertEqual(int(frame[0, 0, 0]), 150)
        self.assertTrue(capture.released)

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
        self.assertEqual(round(resolved["abs_ts"], 1), round(base + 2.0, 1))
        boxes = json.loads(resolved["boxes_json"])
        self.assertEqual(boxes[0]["cls"], "person")

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

    def test_yolo_tag_video_stores_decoded_bitc_time(self) -> None:
        """Pixel-marked segment: backfill reads the marker from the frame,
        stores its exact time, and masks the strip from YOLO's input.
        (Reads via PyAV now — cv2 discarded SEI side data — and the clock must
        sit inside the segment's own span, so the fake epoch matches.)"""
        import av

        unix_seconds = 1_781_600_100.12
        model = _FakeModel()
        db = _FakeVideoDB(media_epoch=unix_seconds - 0.5)
        with tempfile.TemporaryDirectory(prefix="wanyard-yolo-tag-") as tmp:
            path = Path(tmp) / "segment.mp4"
            out = av.open(str(path), "w", format="mp4")
            vs = out.add_stream("libx264", rate=5)
            vs.width, vs.height, vs.pix_fmt = 640, 360, "yuv420p"
            vs.options = {"bf": "0", "g": "5", "crf": "18"}
            img = np.full((360, 640, 3), 180, dtype=np.uint8)
            bitc.render(img, bitc.encode_value(unix_seconds))
            for _ in range(3):
                for p in vs.encode(av.VideoFrame.from_ndarray(img, format="bgr24")):
                    out.mux(p)
            for p in vs.encode(None):
                out.mux(p)
            out.close()

            with mock.patch("wanyard.video._parse_results",
                            return_value=(False, 0.0, [])):
                stored = _yolo_tag_video(model, path, 7, db)

        self.assertEqual(stored, 1)   # 3 frames at 5fps = one 1s sample window
        self.assertEqual(db.scanned, [7])
        assert db.detections is not None
        self.assertEqual(round(db.detections[0]["abs_ts"] * 100),
                         bitc.encode_value(unix_seconds))
        self.assertEqual(len(model.frames), 1)
        x0, y0, width, height = bitc.geometry(model.frames[0])
        self.assertTrue(np.all(model.frames[0][y0 : y0 + height, x0 : x0 + width, :] == 0))


class BitcRoundTripInvariantTests(unittest.TestCase):
    """check_detection_round_trip: resolve(media->BITC(detection)) must land on a
    usable asset whose BITC time equals the detection's, within EPS. The invariant
    is on BITC time, not offset (a boundary-duplicate instant resolves to the
    adjacent segment at a different offset and is still correct)."""

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
            anchor=types.SimpleNamespace(media_to_bitc=lambda off: epoch + off),
            media_offset=media_offset,
            reason="ok",
        )

    def test_ok_when_resolved_bitc_matches(self) -> None:
        conn = self._conn(media_epoch=1000.0, abs_ts=1005.0)   # expected offset 5.0
        with mock.patch.object(media_time, "resolve", return_value=self._loc(1000.0, 5.0)):
            rt = media_time.check_detection_round_trip(conn, Path("/x"), 1)
        self.assertTrue(rt.ok)
        self.assertEqual(rt.status, "ok")
        self.assertFalse(rt.alternate)
        self.assertLess(rt.world_delta, media_time.EPS)

    def test_alternate_segment_same_instant_is_ok(self) -> None:
        # boundary duplicate: adjacent segment (epoch 1002, offset 3) -> same BITC
        conn = self._conn(media_epoch=1000.0, abs_ts=1005.0)   # expected offset 5.0
        with mock.patch.object(media_time, "resolve", return_value=self._loc(1002.0, 3.0)):
            rt = media_time.check_detection_round_trip(conn, Path("/x"), 1)
        self.assertTrue(rt.ok)
        self.assertTrue(rt.alternate)                          # offset differs, BITC matches

    def test_world_mismatch_when_resolved_bitc_drifts(self) -> None:
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
