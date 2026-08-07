from __future__ import annotations

import json
import os
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
    def test_density_presets_relax_confidence_without_calling_noise_verified(self):
        world = np.arange(15, dtype=np.float32).reshape(1, 1, 5, 3)
        confidence = np.asarray([[[-1.0, 1.0, 2.0, 3.0, 4.0]]], dtype=np.float32)
        colors = np.zeros((1, 1, 5, 3), dtype=np.uint8)
        valid = np.ones((1, 1, 5), dtype=bool)

        _, _, _, standard = reconstruction._select_vggt_points(
            np, world, confidence, colors, 100, valid,
            confidence_percentile=45,
        )
        _, _, _, high = reconstruction._select_vggt_points(
            np, world, confidence, colors, 100, valid,
            confidence_percentile=20,
        )
        _, _, _, full = reconstruction._select_vggt_points(
            np, world, confidence, colors, 100, valid,
            confidence_percentile=0,
        )

        self.assertEqual(standard["confidence_kept"], 2)
        self.assertEqual(high["confidence_kept"], 3)
        self.assertEqual(full["confidence_kept"], 5)
        self.assertIsNone(full["confidence_threshold"])

    def test_worker_passes_the_persisted_density_to_reconstruction(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SpatialStore(Path(directory) / "spatial")
            manifest = store.create_scene(
                "Front", ["front", "garden"], density_preset="full"
            )
            with mock.patch.object(
                reconstruction, "reconstruct_run", return_value={}
            ) as reconstruct:
                found = reconstruction.process_next_run(
                    store, object(), Path(directory) / "video"
                )

            self.assertTrue(found)
            self.assertEqual(reconstruct.call_args.kwargs["point_budget"], 2_000_000)
            self.assertEqual(reconstruct.call_args.kwargs["density_preset"], "full")
            self.assertEqual(reconstruct.call_args.kwargs["confidence_percentile"], 0)
            self.assertEqual(
                reconstruct.call_args.args[4], manifest["run"]["id"]
            )

    def test_depth_edge_mask_marks_relative_discontinuities(self):
        depth = np.ones((1, 5, 5), dtype=np.float32)
        depth[0, 2, 2] = 2.0

        mask = reconstruction._depth_edge_mask(np, depth, rtol=0.03)

        self.assertFalse(mask[0, 0, 0])
        self.assertTrue(mask[0, 2, 2])
        self.assertTrue(mask[0, 1, 1])
        self.assertFalse(mask[0, 4, 4])

    def test_cross_view_filter_rejects_a_point_floating_before_shared_depth(self):
        height = width = 3
        depth = np.full((2, height, width), 2.0, dtype=np.float32)
        yy, xx = np.mgrid[0:height, 0:width]
        base = np.stack([
            (xx - 1) * depth[0], (yy - 1) * depth[0], depth[0]
        ], axis=-1).astype(np.float32)
        points = np.stack([base.copy(), base.copy()])
        points[0, 1, 1] = [0.0, 0.0, 1.0]
        extrinsic = np.repeat(
            np.asarray([[[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]]], dtype=np.float32),
            2, axis=0,
        )
        intrinsic = np.repeat(
            np.asarray([[[1, 0, 1], [0, 1, 1], [0, 0, 1]]], dtype=np.float32),
            2, axis=0,
        )

        mask = reconstruction._cross_view_consistency_mask(
            np, points, depth, extrinsic, intrinsic
        )

        self.assertFalse(mask[0, 1, 1])
        self.assertTrue(mask[0, 0, 0])
        self.assertTrue(mask[1].all())

    def test_spatial_support_filter_removes_sparse_outlier(self):
        class BruteTree:
            def __init__(self, values):
                self.values = values

            def query(self, values, k, workers):
                distances = np.linalg.norm(
                    values[:, None, :] - self.values[None, :, :], axis=2
                )
                return np.sort(distances, axis=1)[:, :k], None

        cluster = np.asarray([
            [x, y, 0.0] for x in (0.0, 0.1, 0.2) for y in (0.0, 0.1, 0.2)
        ], dtype=np.float32)
        points = np.concatenate([cluster, [[10.0, 10.0, 10.0]]], axis=0)

        keep, stats = reconstruction._spatial_support_mask(
            np, points, neighbors=3, mad_multiplier=3.0,
            tree_factory=BruteTree,
        )

        self.assertTrue(keep[:-1].all())
        self.assertFalse(keep[-1])
        self.assertEqual(stats["spatial_outliers_removed"], 1)

    def test_reconstruction_steps_back_from_an_undecodable_segment_tail(self):
        frame = np.zeros((4, 4, 3), dtype=np.uint8)

        def read_frame(_db, _directory, camera_id, timestamp):
            if timestamp == 100.0:
                return SimpleNamespace(frame=None, status="pending")
            return SimpleNamespace(frame=frame + (camera_id == "garden"), status="ok")

        with mock.patch.object(reconstruction.stereo, "_read_frame", side_effect=read_frame):
            timestamp, frames, camera_ids, unavailable = (
                reconstruction._read_reconstruction_frames(
                    object(), Path("video"), ["front", "garden"],
                    "front", "garden", 100.0,
                )
            )

        self.assertEqual(timestamp, 95.0)
        self.assertEqual(camera_ids, ["front", "garden"])
        self.assertEqual(len(frames), 2)
        self.assertEqual(unavailable, [])

    def test_live_map_preserves_point_order_and_camera_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live-map.bin"
            uv = np.asarray([[0.125, 0.25], [0.75, 0.875]], dtype=np.float32)
            cameras = np.asarray([0, 1], dtype=np.uint8)

            reconstruction._write_live_map(path, uv, cameras, 2, np)

            payload = path.read_bytes()
            self.assertEqual(payload[:4], b"WYLM")
            self.assertEqual(int.from_bytes(payload[4:6], "little"), 1)
            self.assertEqual(int.from_bytes(payload[6:8], "little"), 2)
            self.assertEqual(int.from_bytes(payload[8:12], "little"), 2)
            records = np.frombuffer(
                payload[12:],
                dtype=[("u", "<f4"), ("v", "<f4"), ("camera", "u1"), ("padding", "u1", (3,))],
            )
            np.testing.assert_allclose(records["u"], uv[:, 0])
            np.testing.assert_allclose(records["v"], uv[:, 1])
            np.testing.assert_array_equal(records["camera"], cameras)

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
                faces=np.asarray([[0, 1, 2]], dtype=np.int32),
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
                mock.patch.dict(os.environ, {"WANYARD_SPATIAL_ENGINE": "opencv"}),
                mock.patch.object(
                    reconstruction.stereo, "latest_decodable_pair",
                    return_value=(123.0, {"front": frame, "garden": frame}),
                ),
                mock.patch.object(reconstruction.stereo, "_read_frame", return_value=frame),
                mock.patch.object(
                    reconstruction, "build_projective_cloud", return_value=cloud
                ) as build_cloud,
            ):
                result = reconstruction.reconstruct_run(
                    store, object(), root / "video", scene_id, run_id,
                    ["front", "garden"],
                )

            self.assertEqual(result["run"]["status"], "ready")
            self.assertEqual(result["run"]["kind"], "opencv_projective")
            self.assertEqual(result["stats"]["points"], 3)
            self.assertEqual(result["stats"]["point_budget"], 500_000)
            self.assertEqual(result["stats"]["density_preset"], "high")
            self.assertEqual(result["stats"]["confidence_percentile"], 20)
            self.assertEqual(build_cloud.call_args.kwargs["max_points"], 500_000)
            self.assertEqual(result["stats"]["faces"], 1)
            self.assertEqual(result["warnings"], [])
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
                mock.patch.dict(os.environ, {"WANYARD_SPATIAL_ENGINE": "opencv"}),
                mock.patch.object(
                    reconstruction.stereo, "latest_decodable_pair",
                    return_value=(123.0, {"front": frame, "garden": frame}),
                ),
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
