from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard.spatial import SpatialStore
from wanyard import spatial_reconstruction as reconstruction


class SpatialReconstructionTests(unittest.TestCase):
    def test_choose_pair_uses_strongest_usable_feasibility_edge(self):
        pair = reconstruction._choose_pair(["a", "b", "c"], {"pairs": [
            {"left_camera_id": "a", "right_camera_id": "b", "status": "borderline", "metrics": {"score": 4}},
            {"left_camera_id": "b", "right_camera_id": "c", "status": "promising", "metrics": {"score": 9}},
            {"left_camera_id": "a", "right_camera_id": "c", "status": "weak", "metrics": {"score": 100}},
        ]})
        self.assertEqual(pair, ("b", "c"))

    def test_reconstruct_run_publishes_browser_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SpatialStore(root / "spatial")
            manifest = store.create_scene("Front", ["front", "garden"])
            scene_id = manifest["scene"]["id"]
            run_id = manifest["run"]["id"]
            points = np.asarray([
                [-1.0, -0.5, 2.0], [0.0, 0.0, 2.5], [1.0, 0.5, 3.0],
            ], dtype=np.float32)
            colors = np.asarray([
                [255, 0, 0], [0, 255, 0], [0, 0, 255],
            ], dtype=np.uint8)
            cloud = reconstruction.ProjectiveCloud(
                points=points,
                colors=colors,
                rectified_left=np.full((32, 48, 3), 60, dtype=np.uint8),
                sample_x=np.asarray([10, 20, 30]),
                sample_y=np.asarray([10, 15, 20]),
                sample_depth=points[:, 2],
                timestamp=123.0,
                left_camera_id="front",
                right_camera_id="garden",
                metrics={"fundamental_inliers": 30},
                pose_rotation=np.eye(3),
                pose_translation=np.asarray([[1.0], [0.0], [0.0]]),
                left_intrinsic=np.eye(3),
                right_intrinsic=np.eye(3),
                disparity_range=(-16, 64),
            )
            frame = SimpleNamespace(frame=np.zeros((8, 8, 3), dtype=np.uint8), status="ok")
            with (
                mock.patch.object(reconstruction.stereo, "latest_common_timestamp", return_value=123.0),
                mock.patch.object(reconstruction.stereo, "_read_frame", return_value=frame),
                mock.patch.object(reconstruction, "build_projective_cloud", return_value=cloud),
            ):
                result = reconstruction.reconstruct_run(
                    store, object(), root / "video", scene_id, run_id,
                    ["front", "garden"],
                )

            self.assertEqual(result["run"]["status"], "ready")
            self.assertEqual(result["run"]["kind"], "opencv_projective")
            self.assertEqual(result["stats"]["points"], 3)
            run_dir = store.run_directory(scene_id, run_id)
            for relative in result["artifacts"].values():
                self.assertTrue((run_dir / relative).is_file())
            self.assertIn(
                b"format binary_little_endian 1.0",
                (run_dir / "scene.ply").read_bytes()[:200],
            )
            summary = json.loads((run_dir / "model-summary.json").read_text())
            self.assertEqual(summary["method"], "opencv-projective-sgbm")

    def test_failed_reconstruction_is_visible_in_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SpatialStore(Path(directory) / "spatial")
            manifest = store.create_scene("Front", ["front", "garden"])
            frame = SimpleNamespace(frame=object(), status="ok")
            with (
                mock.patch.object(reconstruction.stereo, "latest_common_timestamp", return_value=123.0),
                mock.patch.object(reconstruction.stereo, "_read_frame", return_value=frame),
                mock.patch.object(
                    reconstruction,
                    "build_projective_cloud",
                    side_effect=reconstruction.SpatialReconstructionError("no stable points"),
                ),
                self.assertRaisesRegex(reconstruction.SpatialReconstructionError, "no stable points"),
            ):
                reconstruction.reconstruct_run(
                    store, object(), Path(directory),
                    manifest["scene"]["id"], manifest["run"]["id"],
                    ["front", "garden"],
                )

            run = store.list_scenes()[0]["runs"][0]
            self.assertEqual(run["status"], "failed")
            self.assertEqual(run["error"], "no stable points")
            # list_scenes intentionally exposes the failure through run fields.
            raw = json.loads((store.run_directory(
                manifest["scene"]["id"], manifest["run"]["id"]
            ) / "manifest.json").read_text())
            self.assertEqual(raw["error"], "no stable points")


if __name__ == "__main__":
    unittest.main()
