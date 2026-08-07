from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wanyard import spatial_feasibility as feasibility
from wanyard.stereo import MatchMetrics


class SpatialFeasibilityTests(unittest.TestCase):
    def test_sparse_strong_geometry_is_a_neural_candidate(self):
        metrics = SimpleNamespace(
            fundamental_inliers=7,
            inlier_ratio=0.70,
            left_grid_coverage=0.125,
            right_grid_coverage=0.145833,
            median_epipolar_error_px=0.000047,
        )
        self.assertTrue(feasibility._is_neural_candidate(metrics))

    def test_sparse_local_or_noisy_geometry_remains_weak(self):
        base = dict(
            fundamental_inliers=7,
            inlier_ratio=0.70,
            left_grid_coverage=0.125,
            right_grid_coverage=0.145833,
            median_epipolar_error_px=0.1,
        )
        for changed in (
            {"fundamental_inliers": 6},
            {"inlier_ratio": 0.40},
            {"left_grid_coverage": 0.05},
            {"median_epipolar_error_px": 2.0},
        ):
            self.assertFalse(feasibility._is_neural_candidate(
                SimpleNamespace(**{**base, **changed})
            ))

    def test_validation_requires_unique_bounded_camera_ids(self):
        for ids in (["a"], ["a", "a"], ["" , "b"], list(map(str, range(17)))):
            with self.assertRaises(feasibility.SpatialFeasibilityError):
                feasibility.inspect_camera_set(object(), "/video", ids)

    def test_components_connect_on_promising_or_borderline_pairs(self):
        pairs = [
            {"left_camera_id": "a", "right_camera_id": "b", "status": "promising"},
            {"left_camera_id": "b", "right_camera_id": "c", "status": "borderline"},
            {"left_camera_id": "a", "right_camera_id": "c", "status": "weak"},
            {"left_camera_id": "c", "right_camera_id": "d", "status": "error"},
        ]
        self.assertEqual(feasibility._connected_components(["a", "b", "c", "d"], pairs), [["a", "b", "c"], ["d"]])

    @patch("wanyard.spatial_feasibility._inspect_pair")
    def test_report_is_mergeable_through_indirect_overlap(self, inspect_pair):
        inspect_pair.side_effect = [
            {"left_camera_id": "a", "right_camera_id": "b", "status": "promising"},
            {"left_camera_id": "a", "right_camera_id": "c", "status": "weak"},
            {"left_camera_id": "b", "right_camera_id": "c", "status": "borderline"},
        ]
        report = feasibility.inspect_camera_set(object(), "/video", ["a", "b", "c"])
        self.assertTrue(report["mergeable"])
        self.assertEqual(report["status"], "mergeable")
        self.assertEqual(report["components"], [["a", "b", "c"]])

    @patch("wanyard.spatial_feasibility._analyze_frames")
    @patch("wanyard.spatial_feasibility.stereo._read_frame")
    @patch("wanyard.spatial_feasibility.stereo.latest_common_timestamp")
    def test_pair_error_is_recorded_without_geometry(self, latest, read_frame, analyze):
        latest.return_value = 100.0
        read_frame.side_effect = [SimpleNamespace(frame=None, status="not_found"), SimpleNamespace(frame=object(), status="ok")]
        pair = feasibility._inspect_pair(object(), "/video", "a", "b", 960)
        self.assertEqual(pair["status"], "error")
        self.assertIsNone(pair["metrics"])
        analyze.assert_not_called()

    @patch("wanyard.spatial_feasibility._analyze_frames")
    @patch("wanyard.spatial_feasibility.stereo._read_frame")
    @patch("wanyard.spatial_feasibility.stereo.latest_common_timestamp", return_value=100.0)
    def test_pair_serializes_metrics_and_classification(self, _latest, read_frame, analyze):
        read_frame.return_value = SimpleNamespace(frame=object(), status="ok")
        analyze.return_value = SimpleNamespace(metrics=MatchMetrics(100, 100, 90, 80, .8, .3, .3, 1.0, 90.0))
        pair = feasibility._inspect_pair(object(), "/video", "a", "b", 960)
        self.assertEqual(pair["status"], "promising")
        self.assertEqual(pair["timestamp"], 100.0)
        self.assertEqual(pair["metrics"]["fundamental_inliers"], 80)

    @patch("wanyard.spatial_feasibility._analyze_frames")
    @patch("wanyard.spatial_feasibility.stereo._read_frame")
    @patch("wanyard.spatial_feasibility.stereo.latest_common_timestamp", return_value=100.0)
    def test_pair_promotes_sparse_consensus_for_neural_attempt(self, _latest, read_frame, analyze):
        read_frame.return_value = SimpleNamespace(frame=object(), status="ok")
        analyze.return_value = SimpleNamespace(
            metrics=MatchMetrics(4441, 2476, 10, 7, .7, .125, .145833, .000047, 5.249953)
        )
        pair = feasibility._inspect_pair(object(), "/video", "garden", "desk", 960)
        self.assertEqual(pair["status"], "borderline")
        self.assertIn("neural reconstruction", pair["reasons"][0])


if __name__ == "__main__":
    unittest.main()
