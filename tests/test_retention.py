from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard import retention
from wanyard.db import SourceDB, RtspSourceRow
from wanyard.video import VideoSegmentDB


def _source_db(tmpdir: str, *ids: str) -> SourceDB:
    db = SourceDB(Path(tmpdir) / "sources.db")
    for sid in ids:
        db.insert(RtspSourceRow(
            id=sid, name=sid, url=f"rtsp://cam/{sid}",
            interval_seconds=60, enabled=True,
            rtsp_transport="tcp", timeout_seconds=10, output_subdir=sid,
        ))
    return db


class RecordModeTests(unittest.TestCase):
    def test_normalize(self) -> None:
        self.assertEqual(retention.normalize_record_mode("live_only"), "live_only")
        self.assertEqual(retention.normalize_record_mode("Live-Only"), "live_only")
        self.assertEqual(retention.normalize_record_mode("view only"), "live_only")
        self.assertEqual(retention.normalize_record_mode("continuous"), "continuous")
        self.assertIsNone(retention.normalize_record_mode("bogus"))
        self.assertEqual(retention.normalize_record_mode(None, "continuous"), "continuous")

    def test_validate_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            retention.validate_record_mode("sometimes")

    def test_default_is_continuous(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = _source_db(tmpdir, "front")
            self.assertEqual(retention.record_mode(db, "front"), "continuous")
            self.assertFalse(retention.is_live_only(db, "front"))

    def test_live_only_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = _source_db(tmpdir, "front")
            db.set_setting(retention.record_mode_key("front"), "live_only")
            self.assertTrue(retention.is_live_only(db, "front"))

    def test_garbage_setting_defaults_to_continuous(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = _source_db(tmpdir, "front")
            db.set_setting(retention.record_mode_key("front"), "whatever")
            self.assertFalse(retention.is_live_only(db, "front"))


class DaysTests(unittest.TestCase):
    def test_normalize_days(self) -> None:
        self.assertEqual(retention.normalize_days("3"), 3.0)
        self.assertEqual(retention.normalize_days(1.5), 1.5)
        self.assertIsNone(retention.normalize_days(None))
        self.assertIsNone(retention.normalize_days(""))
        self.assertIsNone(retention.normalize_days("global"))
        self.assertIsNone(retention.normalize_days(0))
        self.assertIsNone(retention.normalize_days(-2))
        self.assertIsNone(retention.normalize_days("nan"))
        self.assertIsNone(retention.normalize_days("junk"))

    def test_source_cleanup_days_reads_only_valid_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_db = VideoSegmentDB(Path(tmpdir) / "video.db")
            video_db.set_setting(retention.cleanup_days_key("front"), 3)
            video_db.set_setting(retention.cleanup_days_key("garden"), "junk")
            video_db.set_setting("cleanup_days", 30)
            self.assertEqual(retention.source_cleanup_days(video_db), {"front": 3.0})


class PayloadTests(unittest.TestCase):
    def test_payload_shapes_effective_days_and_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source_db = _source_db(tmpdir, "front", "garden")
            video_db = VideoSegmentDB(Path(tmpdir) / "video.db")
            video_db.set_setting("cleanup_days", 30)
            video_db.set_setting(retention.cleanup_days_key("garden"), 3)
            source_db.set_setting(retention.record_mode_key("garden"), "live_only")
            sources = [{"id": "front"}, {"id": "garden"}]

            payload = retention.retention_settings_payload(source_db, video_db, sources)

            self.assertEqual(payload["cleanup_days"], 30.0)
            self.assertEqual(payload["day_overrides"], {"garden": 3.0})
            self.assertEqual(payload["record_modes"],
                             {"front": "continuous", "garden": "live_only"})
            self.assertEqual(payload["effective_days"],
                             {"front": 30.0, "garden": 3.0})


class CaptureWorkerGatingTests(unittest.TestCase):
    def test_live_only_source_gets_no_recorder_and_stops_on_flip(self) -> None:
        from wanyard.runner import CaptureWorker

        with tempfile.TemporaryDirectory() as tmpdir:
            source_db = _source_db(tmpdir, "front", "garden")
            source_db.set_setting(retention.record_mode_key("garden"), "live_only")
            video_db = VideoSegmentDB(Path(tmpdir) / "video.db")

            with mock.patch("wanyard.runner.VideoWorker") as worker_cls:
                worker_cls.return_value = mock.Mock()
                cw = CaptureWorker(source_db, Path(tmpdir), video_db)
                with mock.patch.object(cw, "_spawn"):
                    cw._sync_sources()
                    self.assertIn("front", cw.video_workers)
                    self.assertNotIn("garden", cw.video_workers)

                    # continuous -> live_only stops the worker
                    source_db.set_setting(
                        retention.record_mode_key("front"), "live_only")
                    cw._sync_sources()
                    self.assertNotIn("front", cw.video_workers)

                    # live_only -> continuous starts one again
                    source_db.delete_setting(retention.record_mode_key("front"))
                    cw._sync_sources()
                    self.assertIn("front", cw.video_workers)

    def test_settings_error_defaults_to_recording(self) -> None:
        from wanyard.runner import CaptureWorker

        with tempfile.TemporaryDirectory() as tmpdir:
            source_db = _source_db(tmpdir, "front")
            video_db = VideoSegmentDB(Path(tmpdir) / "video.db")
            with mock.patch("wanyard.runner.VideoWorker") as worker_cls:
                worker_cls.return_value = mock.Mock()
                cw = CaptureWorker(source_db, Path(tmpdir), video_db)
                with mock.patch.object(cw, "_spawn"), \
                     mock.patch("wanyard.runner.is_live_only",
                                side_effect=RuntimeError("db locked")):
                    cw._sync_sources()
                    self.assertIn("front", cw.video_workers)


class StamperGatingTests(unittest.TestCase):
    def test_live_only_path_is_not_stamped(self) -> None:
        from wanyard.stamper import _StamperSupervisor

        with tempfile.TemporaryDirectory() as tmpdir:
            source_db = _source_db(tmpdir, "front", "garden")
            source_db.set_setting(retention.record_mode_key("garden"), "live_only")

            sup = _StamperSupervisor(
                threading.Event(), source_db_path=source_db.path)
            sup.health_store = None
            with mock.patch("wanyard.stamper._list_relay_paths",
                            return_value=["front", "garden", "front-stamped"]), \
                 mock.patch.object(sup, "_make_worker") as make_worker:
                make_worker.return_value = mock.Mock()
                sup._sync_paths()
                self.assertIn("front", sup.workers)
                self.assertNotIn("garden", sup.workers)
                self.assertNotIn("front-stamped", sup.workers)

                # flip front to live_only: worker stopped on next sync
                source_db.set_setting(
                    retention.record_mode_key("front"), "live_only")
                sup._sync_paths()
                self.assertNotIn("front", sup.workers)


class _OneShotEvent(threading.Event):
    """stop_event whose first wait() ends the loop — runs exactly one cycle."""

    def wait(self, timeout=None):  # noqa: ARG002
        self.set()
        return True


class CleanupLoopTests(unittest.TestCase):
    def _seg(self, video_db, video_dir, source_id, age_days, name) -> tuple[int, Path]:
        now = time.time()
        rel = f"{source_id}/{name}.mp4"
        p = video_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 64)
        seg_id = video_db.open_segment(source_id, rel, now - age_days * 86400 - 60)
        video_db.close_segment(seg_id, now - age_days * 86400, None, None)
        return seg_id, p

    def _notif(self, video_db, source_id, age_days) -> None:
        now = time.time()
        with video_db._connect() as conn:
            rule_id = conn.execute(
                "INSERT INTO notification_rules(name, source_id) VALUES(?,?)",
                (f"rule-{source_id}", source_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO notification_events"
                " (rule_id, rule_name, source_id, zone_ref, event_ref, event_ts,"
                "  class, title, body)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (rule_id, "r", source_id, "whole_frame",
                 f"{source_id}-{age_days}", now - age_days * 86400,
                 "person", "t", "b"),
            )

    def _notif_ages(self, video_db) -> dict[str, list[float]]:
        now = time.time()
        with video_db._connect() as conn:
            rows = conn.execute(
                "SELECT source_id, event_ts FROM notification_events").fetchall()
        out: dict[str, list[float]] = {}
        for r in rows:
            out.setdefault(r["source_id"], []).append((now - r["event_ts"]) / 86400)
        return out

    def test_per_source_horizons(self) -> None:
        from wanyard.yolo_server import _cleanup_loop

        with tempfile.TemporaryDirectory() as tmpdir:
            video_dir = Path(tmpdir) / "video"
            video_dir.mkdir()
            video_db = VideoSegmentDB(Path(tmpdir) / "video.db")
            video_db.set_setting("cleanup_days", 30)
            video_db.set_setting(retention.cleanup_days_key("garden"), 2)

            # front (global 30d): 40d old should die, 10d old survives
            _, front_old = self._seg(video_db, video_dir, "front", 40, "old")
            _, front_new = self._seg(video_db, video_dir, "front", 10, "new")
            # garden (override 2d): 10d old dies despite being < 30d
            _, garden_old = self._seg(video_db, video_dir, "garden", 10, "old")
            _, garden_new = self._seg(video_db, video_dir, "garden", 1, "new")

            self._notif(video_db, "front", 40)
            self._notif(video_db, "front", 10)
            self._notif(video_db, "garden", 10)
            self._notif(video_db, "garden", 1)

            _cleanup_loop(video_db, video_dir, _OneShotEvent())

            self.assertFalse(front_old.exists())
            self.assertTrue(front_new.exists())
            self.assertFalse(garden_old.exists())
            self.assertTrue(garden_new.exists())

            with video_db._connect() as conn:
                remaining = {
                    r["path"] for r in
                    conn.execute("SELECT path FROM segments").fetchall()
                }
            self.assertEqual(remaining, {"front/new.mp4", "garden/new.mp4"})

            # Notifications expire on each camera's own horizon: garden's
            # 2-day override must not touch front's 10-day notification.
            ages = self._notif_ages(video_db)
            self.assertEqual(len(ages.get("front", [])), 1)
            self.assertAlmostEqual(ages["front"][0], 10, delta=0.1)
            self.assertEqual(len(ages.get("garden", [])), 1)
            self.assertAlmostEqual(ages["garden"][0], 1, delta=0.1)

    def test_no_global_days_still_applies_overrides(self) -> None:
        from wanyard.yolo_server import _cleanup_loop

        with tempfile.TemporaryDirectory() as tmpdir:
            video_dir = Path(tmpdir) / "video"
            video_dir.mkdir()
            video_db = VideoSegmentDB(Path(tmpdir) / "video.db")
            video_db.set_setting(retention.cleanup_days_key("garden"), 2)

            _, front_old = self._seg(video_db, video_dir, "front", 400, "old")
            _, garden_old = self._seg(video_db, video_dir, "garden", 10, "old")

            with mock.patch.dict("os.environ", {"CLEANUP_DAYS": "",
                                                "CLEANUP_MAX_GB": ""}):
                _cleanup_loop(video_db, video_dir, _OneShotEvent())

            # No global horizon: front is never age-pruned; garden's override applies.
            self.assertTrue(front_old.exists())
            self.assertFalse(garden_old.exists())


if __name__ == "__main__":
    unittest.main()
