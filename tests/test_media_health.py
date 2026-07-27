from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard.media_health import (
    MediaHealthCollector,
    MediaHealthStore,
    parse_mediamtx_metrics,
    parse_rate,
)


class MediaHealthTests(unittest.TestCase):
    @staticmethod
    def _sample(source_id: str, ts: float) -> dict:
        return {
            "ts": ts,
            "source_id": source_id,
            "raw_ready": 1,
            "stamped_ready": 1,
            "raw_bitrate_bps": 1_000_000,
            "stamped_bitrate_bps": 900_000,
            "hls_age_seconds": 0.5,
            "recorder_thread_alive": 1,
            "recorder_codec": "h264",
            "segment_started_ts": ts - 10,
            "segment_completed_ts": ts - 1,
            "consecutive_failures": 0,
            "last_failure_kind": None,
            "active_encoder": "h264_nvenc",
        }

    def test_parse_mediamtx_paths_and_rate(self) -> None:
        metrics = parse_mediamtx_metrics(
            '\n'.join(
                [
                    'paths{name="garden",state="ready"} 1',
                    'paths_bytes_received{name="garden",state="ready"} 1000',
                    'paths{name="garden-stamped",state="notReady"} 0',
                    'paths_bytes_received{name="garden-stamped",state="notReady"} 250',
                ]
            )
        )
        self.assertTrue(metrics["garden"]["ready"])
        self.assertEqual(metrics["garden"]["bytes_received"], 1000)
        self.assertFalse(metrics["garden-stamped"]["ready"])
        self.assertEqual(parse_rate("2.5M"), 2_500_000)
        self.assertEqual(parse_rate("10M"), 10_000_000)
        self.assertIsNone(parse_rate("auto"))

    def test_collector_calculates_counter_delta_bitrate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MediaHealthStore(Path(tmp) / "health.db")
            store.update_stamper_state(
                "garden",
                status="live",
                active_encoder="h264_nvenc",
                maxrate="2.5M",
            )
            readings = iter(
                [
                    (
                        'paths{name="garden",state="ready"} 1\n'
                        'paths_bytes_received{name="garden",state="ready"} 1000\n'
                        'paths{name="garden-stamped",state="ready"} 1\n'
                        'paths_bytes_received{name="garden-stamped",state="ready"} 2000'
                    ),
                    (
                        'paths{name="garden",state="ready"} 1\n'
                        'paths_bytes_received{name="garden",state="ready"} 3000\n'
                        'paths{name="garden-stamped",state="ready"} 1\n'
                        'paths_bytes_received{name="garden-stamped",state="ready"} 5000'
                    ),
                ]
            )
            times = iter([100.0, 110.0])
            collector = MediaHealthCollector(
                store,
                fetch_text=lambda _url: next(readings),
                clock=lambda: next(times),
            )
            status = {"garden": {"hls_age_seconds": 0.5}}
            recorder = {
                "garden": {
                    "thread_alive": True,
                    "codec": "h264",
                    "consecutive_failures": 0,
                }
            }
            first = collector.sample(["garden"], status, recorder)[0]
            second = collector.sample(["garden"], status, recorder)[0]

            self.assertIsNone(first["raw_bitrate_bps"])
            self.assertEqual(second["raw_bitrate_bps"], 1600)
            self.assertEqual(second["stamped_bitrate_bps"], 2400)
            self.assertEqual(second["active_encoder"], "h264_nvenc")

    def test_store_records_health_transitions_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MediaHealthStore(Path(tmp) / "health.db")
            now = time.time()
            store.update_stamper_state(
                "garden",
                status="live",
                active_encoder="h264_nvenc",
                width=2560,
                height=1440,
                fps=15,
                maxrate="2.5M",
                ts=now,
            )
            base = {
                "source_id": "garden",
                "raw_bitrate_bps": 1_600_000,
                "stamped_bitrate_bps": 2_400_000,
                "recorder_codec": "h264",
                "segment_started_ts": now - 10,
                "segment_completed_ts": None,
                "last_failure_kind": None,
                "active_encoder": "h264_nvenc",
            }
            store.record_samples(
                [
                    {
                        **base,
                        "ts": now,
                        "raw_ready": 1,
                        "stamped_ready": 1,
                        "hls_age_seconds": 0.5,
                        "recorder_thread_alive": 1,
                        "consecutive_failures": 0,
                    },
                    {
                        **base,
                        "ts": now + 10,
                        "raw_ready": 1,
                        "stamped_ready": 0,
                        "hls_age_seconds": 8.0,
                        "recorder_thread_alive": 1,
                        "consecutive_failures": 1,
                        "last_failure_kind": "ffmpeg_early_exit",
                    },
                ]
            )

            snapshot = store.snapshot(since=now - 60, source_id="garden")
            self.assertEqual(
                snapshot["current"]["garden"]["consecutive_failures"], 1
            )
            self.assertEqual(
                snapshot["current"]["garden"]["stamper"]["active_encoder"],
                "h264_nvenc",
            )
            self.assertEqual(snapshot["current"]["garden"]["maxrate_bps"], 2_500_000)
            kinds = {event["kind"] for event in snapshot["events"]}
            self.assertIn("stamped_ready_offline", kinds)
            self.assertIn("hls_stale", kinds)
            self.assertIn("recorder_failure", kinds)
            self.assertGreaterEqual(len(snapshot["series"]), 1)

    def test_delete_source_removes_all_health_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MediaHealthStore(Path(tmp) / "health.db")
            now = time.time()
            for source_id in ("front", "garden"):
                store.update_stamper_state(
                    source_id,
                    status="live",
                    active_encoder="h264_nvenc",
                    ts=now,
                )
                store.event(
                    source_id,
                    "pipeline",
                    "warning",
                    "test",
                    "test incident",
                    ts=now,
                )
            store.event(
                None,
                "system",
                "info",
                "startup",
                "service started",
                ts=now,
            )
            store.record_samples([
                self._sample("front", now),
                self._sample("garden", now),
            ])

            store.delete_source("garden")

            snapshot = store.snapshot(since=now - 60)
            self.assertEqual(set(snapshot["current"]), {"front"})
            self.assertEqual(
                {row["source_id"] for row in snapshot["series"]},
                {"front"},
            )
            self.assertNotIn(
                "garden",
                {event["source_id"] for event in snapshot["events"]},
            )
            self.assertIn(
                None,
                {event["source_id"] for event in snapshot["events"]},
            )

    def test_prune_sources_cleans_preexisting_removed_camera_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MediaHealthStore(Path(tmp) / "health.db")
            now = time.time()
            for source_id in ("front", "tapo-garden"):
                store.update_stamper_state(
                    source_id,
                    status="live",
                    ts=now,
                )
                store.event(
                    source_id,
                    "pipeline",
                    "info",
                    "test",
                    "test incident",
                    ts=now,
                )
            store.record_samples([
                self._sample("front", now),
                self._sample("tapo-garden", now),
            ])

            store.prune_sources(["front"])

            snapshot = store.snapshot(since=now - 60)
            self.assertEqual(set(snapshot["current"]), {"front"})
            self.assertTrue(all(
                row["source_id"] == "front" for row in snapshot["series"]
            ))
            self.assertTrue(all(
                event["source_id"] == "front" for event in snapshot["events"]
            ))

    def test_collector_forgets_deleted_source_bitrate_counters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MediaHealthStore(Path(tmp) / "health.db")
            collector = MediaHealthCollector(
                store,
                fetch_text=lambda _url: "",
            )
            collector._previous_counters = {
                "garden": (10.0, 100.0),
                "garden-stamped": (10.0, 200.0),
                "front": (10.0, 300.0),
            }

            collector.forget_source("garden")

            self.assertEqual(
                collector._previous_counters,
                {"front": (10.0, 300.0)},
            )


if __name__ == "__main__":
    unittest.main()
