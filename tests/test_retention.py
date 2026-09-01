from __future__ import annotations

import sqlite3
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
        self.last_timeout = timeout
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

    def _confirmation(self, video_db, source_id, age_days) -> None:
        now = time.time()
        with video_db._connect() as conn:
            conn.execute(
                "INSERT INTO notification_confirmations"
                " (strategy_version, event_ref, source_id, event_ts, class, status)"
                " VALUES(?,?,?,?,?,?)",
                ("test", f"{source_id}-{age_days}", source_id,
                 now - age_days * 86400, "person", "confirmed"),
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

    def test_orphan_notifications_are_pruned_after_manual_segment_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_dir = Path(tmpdir) / "video"
            video_dir.mkdir()
            video_db = VideoSegmentDB(Path(tmpdir) / "video.db")
            now = time.time()
            seg_id, _ = self._seg(video_db, video_dir, "front", 1, "kept")
            with video_db._connect() as conn:
                rule_id = conn.execute(
                    "INSERT INTO notification_rules(name, source_id) VALUES(?,?)",
                    ("rule-front", "front"),
                ).lastrowid
                for ref, event_ts in (("kept", now - 86400), ("orphan", now - 172800)):
                    conn.execute(
                        "INSERT INTO notification_events"
                        " (rule_id, rule_name, source_id, zone_ref, event_ref,"
                        "  event_ts, class, title, body) VALUES(?,?,?,?,?,?,?,?,?)",
                        (rule_id, "r", "front", "whole_frame", ref,
                         event_ts, "person", "t", "b"),
                    )

            self.assertEqual(video_db.prune_orphan_notifications()["events"], 1)
            with video_db._connect() as conn:
                self.assertEqual(
                    [r[0] for r in conn.execute(
                        "SELECT event_ref FROM notification_events ORDER BY id")],
                    ["kept"],
                )
                conn.execute("DELETE FROM segments WHERE id=?", (seg_id,))

            self.assertEqual(video_db.prune_orphan_notifications()["events"], 1)
            self.assertEqual(video_db.unread_notification_count(), 0)

    def test_segment_retention_prunes_orphaned_inactive_object_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_dir = Path(tmpdir) / "video"
            video_dir.mkdir()
            video_db = VideoSegmentDB(Path(tmpdir) / "video.db")
            segment_id, media = self._seg(
                video_db, video_dir, "front", 10, "old"
            )
            with video_db._connect() as conn:
                track_id = conn.execute(
                    "INSERT INTO object_tracks"
                    " (source_id,class,cx,cy,area,first_seen,last_seen,"
                    " first_start_off,last_start_off,last_end_off,active,state)"
                    " VALUES('front','person',.5,.5,.1,1,2,0,0,1,0,'gone')"
                ).lastrowid
                conn.execute(
                    "INSERT INTO object_events"
                    " (track_id,segment_id,source_id,abs_ts,display_ts,class,"
                    " event_type,start_off,end_off)"
                    " VALUES(?,?,?,?,?,'person','appeared',0,1)",
                    (track_id, segment_id, "front", 1.0, 1.0),
                )

            result = retention.delete_segments(
                video_db,
                video_dir,
                [{"id": segment_id, "path": str(media.relative_to(video_dir))}],
            )

            self.assertEqual(result["deleted_object_tracks"], 1)
            with video_db._connect() as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM object_tracks").fetchone()[0],
                    0,
                )

    def test_segment_retention_preserves_track_with_a_surviving_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_dir = Path(tmpdir) / "video"
            video_dir.mkdir()
            video_db = VideoSegmentDB(Path(tmpdir) / "video.db")
            old_id, old_media = self._seg(
                video_db, video_dir, "front", 10, "old"
            )
            kept_id, kept_media = self._seg(
                video_db, video_dir, "front", 1, "kept"
            )
            with video_db._connect() as conn:
                track_id = conn.execute(
                    "INSERT INTO object_tracks"
                    " (source_id,class,cx,cy,area,first_seen,last_seen,"
                    " first_start_off,last_start_off,last_end_off,active,state)"
                    " VALUES('front','person',.5,.5,.1,1,2,0,0,1,0,'gone')"
                ).lastrowid
                for segment_id, ts in ((old_id, 1.0), (kept_id, 2.0)):
                    conn.execute(
                        "INSERT INTO object_events"
                        " (track_id,segment_id,source_id,abs_ts,display_ts,class,"
                        " event_type,start_off,end_off)"
                        " VALUES(?,?,?,?,?,'person','appeared',0,1)",
                        (track_id, segment_id, "front", ts, ts),
                    )

            first = retention.delete_segments(
                video_db,
                video_dir,
                [{"id": old_id, "path": str(old_media.relative_to(video_dir))}],
            )
            self.assertEqual(first["deleted_object_tracks"], 0)
            with video_db._connect() as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM object_tracks WHERE id=?", (track_id,)
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT segment_id FROM object_events WHERE track_id=?",
                        (track_id,),
                    ).fetchone()[0],
                    kept_id,
                )

            final = retention.delete_segments(
                video_db,
                video_dir,
                [{"id": kept_id, "path": str(kept_media.relative_to(video_dir))}],
            )
            self.assertEqual(final["deleted_object_tracks"], 1)

    def test_segment_retention_never_deletes_active_track_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_dir = Path(tmpdir) / "video"
            video_dir.mkdir()
            video_db = VideoSegmentDB(Path(tmpdir) / "video.db")
            segment_id, media = self._seg(
                video_db, video_dir, "front", 10, "old"
            )
            with video_db._connect() as conn:
                track_id = conn.execute(
                    "INSERT INTO object_tracks"
                    " (source_id,class,cx,cy,area,first_seen,last_seen,"
                    " first_start_off,last_start_off,last_end_off,active,state)"
                    " VALUES('front','person',.5,.5,.1,1,2,0,0,1,1,'active')"
                ).lastrowid
                conn.execute(
                    "INSERT INTO object_events"
                    " (track_id,segment_id,source_id,abs_ts,display_ts,class,"
                    " event_type,start_off,end_off)"
                    " VALUES(?,?,?,?,?,'person','appeared',0,1)",
                    (track_id, segment_id, "front", 1.0, 1.0),
                )

            result = retention.delete_segments(
                video_db,
                video_dir,
                [{"id": segment_id, "path": str(media.relative_to(video_dir))}],
            )

            self.assertEqual(result["deleted_object_tracks"], 0)
            self.assertEqual(
                video_db.prune_orphan_object_tracks(busy_timeout_ms=1), 0
            )
            with video_db._connect() as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT active FROM object_tracks WHERE id=?", (track_id,)
                    ).fetchone()[0],
                    1,
                )

    def test_cleanup_rechecks_legacy_backlog_after_one_minute(self) -> None:
        from wanyard.yolo_server import _cleanup_loop

        with tempfile.TemporaryDirectory() as tmpdir:
            video_dir = Path(tmpdir) / "video"
            video_dir.mkdir()
            video_db = VideoSegmentDB(Path(tmpdir) / "video.db")
            stop_event = _OneShotEvent()
            drain_result = {
                "deleted": 10_000,
                "batches": 1,
                "elapsed_seconds": 0.2,
                "drained": False,
                "busy": False,
                "stopped": False,
            }
            with mock.patch.dict(
                    "os.environ",
                    {"CLEANUP_DAYS": "", "CLEANUP_MAX_GB": ""}), \
                    mock.patch(
                        "wanyard.retention.drain_orphan_object_tracks",
                        return_value=drain_result,
                    ):
                _cleanup_loop(video_db, video_dir, stop_event)

            self.assertEqual(stop_event.last_timeout, 60)

    def test_manual_global_cleanup_uses_shared_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_dir = Path(tmpdir) / "video"
            video_dir.mkdir()
            video_db = VideoSegmentDB(Path(tmpdir) / "video.db")
            _, front_old = self._seg(video_db, video_dir, "front", 10, "old")
            _, front_new = self._seg(video_db, video_dir, "front", 1, "new")
            _, garden_old = self._seg(video_db, video_dir, "garden", 10, "old")
            _, garden_new = self._seg(video_db, video_dir, "garden", 1, "new")
            sidecar = front_old.with_name(front_old.name + ".clock.json")
            sidecar.write_bytes(b"clock")
            sprite = front_old.with_suffix("")
            sprite.mkdir()
            (sprite / "sheet.jpg").write_bytes(b"sprite")
            for source_id in ("front", "garden"):
                self._notif(video_db, source_id, 10)
                self._notif(video_db, source_id, 1)
                self._confirmation(video_db, source_id, 10)
                self._confirmation(video_db, source_id, 1)

            result = retention.delete_before(
                video_db, video_dir, time.time() - 5 * 86400
            )

            self.assertEqual(result["deleted_segments"], 2)
            self.assertEqual(result["deleted_notifications"], 2)
            self.assertEqual(result["deleted_confirmations"], 2)
            self.assertFalse(front_old.exists())
            self.assertFalse(garden_old.exists())
            self.assertFalse(sidecar.exists())
            self.assertFalse(sprite.exists())
            self.assertTrue(front_new.exists())
            self.assertTrue(garden_new.exists())
            ages = self._notif_ages(video_db)
            self.assertEqual(set(ages), {"front", "garden"})
            self.assertEqual(len(ages["front"]), 1)
            self.assertEqual(len(ages["garden"]), 1)

    def test_manual_source_cleanup_does_not_touch_other_camera(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_dir = Path(tmpdir) / "video"
            video_dir.mkdir()
            video_db = VideoSegmentDB(Path(tmpdir) / "video.db")
            _, front_old = self._seg(video_db, video_dir, "front", 10, "old")
            _, garden_old = self._seg(video_db, video_dir, "garden", 10, "old")
            self._notif(video_db, "front", 10)
            self._notif(video_db, "garden", 10)

            result = retention.delete_before(
                video_db, video_dir, time.time() - 5 * 86400, "garden"
            )

            self.assertEqual(result["deleted_segments"], 1)
            self.assertEqual(result["deleted_notifications"], 1)
            self.assertTrue(front_old.exists())
            self.assertFalse(garden_old.exists())
            self.assertEqual(set(self._notif_ages(video_db)), {"front"})

    def test_delete_source_recordings_purges_only_that_camera(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_dir = Path(tmpdir) / "video"
            video_dir.mkdir()
            video_db = VideoSegmentDB(Path(tmpdir) / "video.db")
            _, front = self._seg(video_db, video_dir, "front", 1, "front")
            _, desk = self._seg(video_db, video_dir, "desk", 1, "desk")
            self._notif(video_db, "front", 1)
            self._notif(video_db, "desk", 1)
            video_db.set_setting(retention.cleanup_days_key("desk"), 3)
            live_dir = video_dir / "live" / "desk"
            live_dir.mkdir(parents=True)
            (live_dir / "index.m3u8").write_bytes(b"live")

            result = retention.delete_source_recordings(
                video_db, video_dir, "desk"
            )

            self.assertEqual(result["deleted_segments"], 1)
            self.assertTrue(front.exists())
            self.assertFalse(desk.exists())
            self.assertFalse((video_dir / "desk").exists())
            self.assertFalse(live_dir.exists())
            self.assertEqual(set(self._notif_ages(video_db)), {"front"})
            self.assertIsNone(
                video_db.get_setting(retention.cleanup_days_key("desk"))
            )

    def test_delete_source_recordings_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_dir = Path(tmpdir) / "video"
            video_dir.mkdir()
            video_db = VideoSegmentDB(Path(tmpdir) / "video.db")
            with self.assertRaises(ValueError):
                retention.delete_source_recordings(
                    video_db, video_dir, "../elsewhere"
                )

    def test_delete_segments_retries_a_busy_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_dir = Path(tmpdir) / "video"
            video_dir.mkdir()
            video_db = VideoSegmentDB(Path(tmpdir) / "video.db")
            segment_id, media = self._seg(
                video_db, video_dir, "desk", 1, "desk"
            )
            original_connect = video_db._connect
            attempts = 0

            def flaky_connect():
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise sqlite3.OperationalError("database is locked")
                return original_connect()

            with mock.patch.object(video_db, "_connect", side_effect=flaky_connect), \
                    mock.patch("wanyard.retention.time.sleep"):
                result = retention.delete_segments(
                    video_db,
                    video_dir,
                    [{"id": segment_id, "path": str(media.relative_to(video_dir))}],
                )

            # Delete transaction (first try locked) + notification prune.
            self.assertEqual(attempts, 3)
            self.assertEqual(result["deleted_segments"], 1)
            self.assertFalse(media.exists())


class OrphanTrackDrainTests(unittest.TestCase):
    def test_drains_multiple_independently_committed_batches(self) -> None:
        video_db = mock.Mock()
        video_db.prune_orphan_object_tracks.side_effect = [2, 2, 1]

        result = retention.drain_orphan_object_tracks(
            video_db,
            batch_size=2,
            time_budget_seconds=10,
            batch_pause_seconds=0,
            busy_timeout_ms=17,
        )

        self.assertEqual(result["deleted"], 5)
        self.assertEqual(result["batches"], 3)
        self.assertTrue(result["drained"])
        self.assertFalse(result["busy"])
        self.assertEqual(
            video_db.prune_orphan_object_tracks.call_args_list,
            [
                mock.call(2, busy_timeout_ms=17),
                mock.call(2, busy_timeout_ms=17),
                mock.call(2, busy_timeout_ms=17),
            ],
        )

    def test_zero_budget_still_makes_one_bounded_batch(self) -> None:
        video_db = mock.Mock()
        video_db.prune_orphan_object_tracks.return_value = 10

        result = retention.drain_orphan_object_tracks(
            video_db,
            batch_size=10,
            time_budget_seconds=0,
            batch_pause_seconds=0,
        )

        self.assertEqual(result["deleted"], 10)
        self.assertEqual(result["batches"], 1)
        self.assertFalse(result["drained"])
        video_db.prune_orphan_object_tracks.assert_called_once()

    def test_busy_database_yields_without_retrying(self) -> None:
        video_db = mock.Mock()
        video_db.prune_orphan_object_tracks.side_effect = sqlite3.OperationalError(
            "database is locked"
        )

        result = retention.drain_orphan_object_tracks(
            video_db,
            batch_pause_seconds=0,
        )

        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["batches"], 0)
        self.assertTrue(result["busy"])
        self.assertFalse(result["drained"])
        video_db.prune_orphan_object_tracks.assert_called_once()


if __name__ == "__main__":
    unittest.main()
