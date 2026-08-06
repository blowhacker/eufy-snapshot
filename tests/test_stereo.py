from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard import cli, stereo
from wanyard.config import AppConfig


def metrics(**overrides) -> stereo.MatchMetrics:
    values = {
        "left_keypoints": 1000,
        "right_keypoints": 900,
        "ratio_matches": 200,
        "fundamental_inliers": 100,
        "inlier_ratio": 0.5,
        "left_grid_coverage": 0.4,
        "right_grid_coverage": 0.3,
        "median_epipolar_error_px": 0.6,
        "score": 75.0,
    }
    values.update(overrides)
    return stereo.MatchMetrics(**values)


class StereoInspectTests(unittest.TestCase):
    def test_offsets_include_zero_and_requested_endpoints(self) -> None:
        self.assertEqual(
            stereo.build_offsets_ms(-125, 140, 100),
            [-125, -25, 0, 75, 140],
        )

    def test_offsets_reject_unbounded_or_backwards_searches(self) -> None:
        for values in ((10, -10, 5), (-5000, 5000, 5), (-10, 10, 0)):
            with self.subTest(values=values):
                with self.assertRaises(stereo.StereoInspectError):
                    stereo.build_offsets_ms(*values)

    def test_latest_common_timestamp_uses_shared_coverage_and_margin(self) -> None:
        db = mock.Mock()
        db.segment_bounds.side_effect = [
            {"from": 100.0, "to": 200.0},
            {"from": 120.0, "to": 190.0},
        ]
        db.list_segments.side_effect = [
            [{
                "start_ts": 175.0, "end_ts": 200.0,
                "media_epoch": 175.0, "duration_sec": 25.0,
            }],
            [{
                "start_ts": 170.0, "end_ts": 190.0,
                "media_epoch": 170.0, "duration_sec": 20.0,
            }],
        ]
        self.assertEqual(
            stereo.latest_common_timestamp(db, "desk", "garden"),
            188.0,
        )

    def test_latest_common_timestamp_rejects_disjoint_sources(self) -> None:
        db = mock.Mock()
        db.segment_bounds.side_effect = [
            {"from": 100.0, "to": 110.0},
            {"from": 120.0, "to": 130.0},
        ]
        with self.assertRaisesRegex(stereo.StereoInspectError, "no overlapping"):
            stereo.latest_common_timestamp(db, "desk", "garden")

    def test_latest_common_timestamp_requires_finalized_overlap(self) -> None:
        db = mock.Mock()
        db.segment_bounds.side_effect = [
            {"from": 100.0, "to": 200.0},
            {"from": 100.0, "to": 200.0},
        ]
        db.list_segments.side_effect = [
            [{"start_ts": 150.0, "end_ts": None, "media_epoch": 150.0}],
            [{"start_ts": 150.0, "end_ts": None, "media_epoch": 150.0}],
        ]
        with self.assertRaisesRegex(stereo.StereoInspectError, "no shared finalized"):
            stereo.latest_common_timestamp(db, "desk", "garden")

    def test_grid_coverage_counts_spatial_cells_not_convex_hull(self) -> None:
        points = [(1, 1), (99, 1), (1, 99), (99, 99), (99, 99)]
        self.assertAlmostEqual(
            stereo._grid_coverage(points, 100, 100, columns=2, rows=2),
            1.0,
        )

    def test_feasibility_classification(self) -> None:
        self.assertEqual(stereo.classify_feasibility(metrics())[0], "promising")
        self.assertEqual(
            stereo.classify_feasibility(metrics(fundamental_inliers=10))[0],
            "weak",
        )
        self.assertEqual(
            stereo.classify_feasibility(metrics(fundamental_inliers=50))[0],
            "borderline",
        )

    def test_rectification_metrics_measure_alignment_and_retention(self) -> None:
        import numpy as np

        left = np.asarray([
            [10.0, 10.0], [20.0, 20.0], [30.0, 30.0], [120.0, 40.0],
        ])
        right = np.asarray([
            [12.0, 10.2], [22.0, 19.7], [32.0, 30.4], [122.0, 40.1],
        ])
        left_valid = np.full((100, 100), 255, dtype=np.uint8)
        right_valid = np.zeros((100, 100), dtype=np.uint8)
        right_valid[:, :60] = 255

        result = stereo._rectification_metrics(
            np, left, right, left_valid, right_valid
        )

        self.assertEqual(result["matched_points"], 4)
        self.assertEqual(result["points_inside_both"], 3)
        self.assertEqual(result["point_retention_ratio"], 0.75)
        self.assertAlmostEqual(result["median_vertical_error_px"], 0.25)
        self.assertEqual(result["common_valid_fraction"], 0.6)
        self.assertEqual(result["valid_area_balance"], 0.6)

    def test_temporal_offset_requires_a_decisive_peak(self) -> None:
        flat = [
            {"offset_ms": offset, "metrics": {"score": score}}
            for offset, score in [(-50, 100), (0, 102), (50, 99)]
        ]
        self.assertFalse(stereo._temporal_observability(flat)["observable"])

        peaked = [
            {"offset_ms": offset, "metrics": {"score": score}}
            for offset, score in [(-50, 10), (0, 12), (50, 30)]
        ]
        result = stereo._temporal_observability(peaked)
        self.assertTrue(result["observable"])
        self.assertEqual(result["suggested_offset_ms"], 50)

    def test_vehicle_tracks_keep_only_contiguous_motion(self) -> None:
        rows = []
        for index, timestamp in enumerate((1.0, 1.5, 2.0, 2.5)):
            x = 0.1 + index * 0.04
            rows.append({
                "abs_ts": timestamp,
                "boxes": [{
                    "cls": "car",
                    "track_id": "moving",
                    "conf": 0.9,
                    "x1": x,
                    "y1": 0.3,
                    "x2": x + 0.1,
                    "y2": 0.4,
                }, {
                    "cls": "car",
                    "track_id": "parked",
                    "conf": 0.9,
                    "x1": 0.7,
                    "y1": 0.3,
                    "x2": 0.8,
                    "y2": 0.4,
                }],
            })

        tracks = stereo._vehicle_tracks(rows, "front")

        self.assertEqual(len(tracks), 1)
        self.assertTrue(tracks[0].uid.startswith("moving:"))
        self.assertGreater(tracks[0].travel, 0.1)

    def test_vehicle_pair_curve_recovers_inter_camera_offset(self) -> None:
        import numpy as np

        def track(uid, clock_offset):
            samples = tuple(
                stereo.VehicleSample(
                    timestamp=physical_time + clock_offset,
                    x=0.5,
                    y=0.2 + physical_time * 0.04,
                    area=0.03,
                )
                for physical_time in np.arange(0.0, 3.01, 0.1)
            )
            return stereo.VehicleTrack(
                uid=uid,
                source_id=uid,
                cls="car",
                samples=samples,
                travel=0.12,
            )

        # This fundamental matrix describes horizontal epipolar lines, so a
        # time error becomes a directly measurable vertical residual.
        fundamental = np.asarray([
            [0.0, 0.0, 0.0],
            [0.0, 0.0, -1.0],
            [0.0, 1.0, 0.0],
        ])
        pair = stereo._vehicle_pair_curve(
            np,
            fundamental,
            track("left", 0.0),
            track("right", 0.12),
            range(-200, 201, 10),
            (1000, 1000),
            (1000, 1000),
        )

        self.assertIsNotNone(pair)
        self.assertEqual(pair.best_offset_ms, 120)
        self.assertLess(pair.median_error_px, 0.01)
        self.assertGreater(pair.sharpness, 10.0)

    def test_vehicle_consensus_enforces_dynamic_stereo_gate(self) -> None:
        import numpy as np

        sample = stereo.VehicleSample(1.0, 0.5, 0.5, 0.1)
        track = stereo.VehicleTrack(
            "track", "source", "car", (sample,), 0.2
        )
        pairs = [
            stereo._VehiclePair(
                left=track,
                right=track,
                best_offset_ms=35 + (index % 3) * 10,
                median_error_px=2.0,
                p90_error_px=4.0,
                samples=8,
                sharpness=1.4,
                curve=((-100, 5.0), (40, 2.0), (100, 5.0)),
            )
            for index in range(24)
        ]

        result = stereo._summarize_vehicle_pairs(np, pairs)

        self.assertTrue(result["observable"])
        self.assertTrue(result["dynamic_3d_ready"])
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["matched_events"], 24)

    def test_parser_exposes_stereo_inspect(self) -> None:
        args = cli.build_parser().parse_args([
            "stereo-inspect", "desk", "garden", "--at", "123.5",
        ])
        self.assertEqual(args.command, "stereo-inspect")
        self.assertEqual(args.left_source, "desk")
        self.assertEqual(args.right_source, "garden")
        self.assertEqual(args.at, 123.5)

    def test_cli_writes_under_data_directory_by_default(self) -> None:
        args = SimpleNamespace(
            left_source="desk",
            right_source="garden",
            at=123.5,
            offset_min_ms=0,
            offset_max_ms=0,
            offset_step_ms=50,
            max_dimension=1280,
            timing_window_minutes=180.0,
            timing_step_ms=10,
            timing_events=30,
            output_dir=None,
        )
        report = {
            "feasibility": "promising",
            "best_metrics": {
                "fundamental_inliers": 100,
                "inlier_ratio": 0.5,
                "left_grid_coverage": 0.3,
                "right_grid_coverage": 0.2,
            },
            "temporal_offset": {
                "observable": False,
                "suggested_offset_ms": None,
                "confidence": "low",
                "matched_events": 0,
                "p95_residual_ms": None,
                "dynamic_3d_ready": False,
                "reason": "static",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(db_path=Path(tmp) / "sources.db")
            with (
                mock.patch.object(cli, "VideoSegmentDB"),
                mock.patch("wanyard.stereo.inspect_pair", return_value=report) as inspect,
                mock.patch("builtins.print"),
            ):
                self.assertEqual(cli.cmd_stereo_inspect(args, config), 0)
            output = inspect.call_args.args[6]
            self.assertEqual(
                output,
                Path(tmp) / "stereo-inspect" / "desk-garden" / "123.500",
            )


if __name__ == "__main__":
    unittest.main()
