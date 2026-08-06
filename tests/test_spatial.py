from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard.spatial import SpatialStore, SpatialStoreError


class SpatialStoreTests(unittest.TestCase):
    def _run(self, root: Path, scene_id="garden", run_id="preview", cameras=None):
        run_dir = root / scene_id / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "cloud.ply").write_bytes(b"ply\n")
        (run_dir / "manifest.json").write_text(json.dumps({
            "scene": {
                "id": scene_id,
                "name": "Front garden",
                "camera_ids": cameras or ["tapo-front", "garden-old"],
            },
            "run": {
                "id": run_id,
                "created_at": "2026-08-06T17:37:00Z",
                "kind": "neural_preview",
                "metric": False,
                "status": "ready",
            },
            "artifacts": {"point_cloud": "cloud.ply"},
            "stats": {"points": 120000},
        }), encoding="utf-8")

    def test_scene_model_accepts_an_arbitrary_camera_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cameras = [f"camera-{index}" for index in range(243)]
            self._run(root, cameras=cameras)

            scenes = SpatialStore(root).list_scenes()

            self.assertEqual(len(scenes), 1)
            self.assertEqual(len(scenes[0]["camera_ids"]), 243)
            self.assertEqual(scenes[0]["runs"][0]["stats"]["points"], 120000)

    def test_create_scene_queues_an_arbitrary_camera_set_and_lists_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cameras = [f"camera-{index}" for index in range(243)]

            manifest = SpatialStore(root).create_scene(
                "  New front-garden view  ", cameras, feasibility_id="check-123"
            )
            scenes = SpatialStore(root).list_scenes()

            self.assertEqual(manifest["scene"]["name"], "New front-garden view")
            self.assertEqual(manifest["scene"]["camera_ids"], cameras)
            self.assertEqual(manifest["run"]["status"], "queued")
            self.assertEqual(manifest["artifacts"], {})
            self.assertEqual(manifest["feasibility"], {"id": "check-123"})
            self.assertEqual(len(scenes), 1)
            self.assertEqual(scenes[0]["runs"][0]["status"], "queued")
            self.assertEqual(scenes[0]["runs"][0]["artifacts"], {})

    def test_create_scene_does_not_derive_paths_from_the_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = SpatialStore(root).create_scene(
                "../../Garden / front", ["tapo-front"]
            )
            scene_id = manifest["scene"]["id"]
            run_id = manifest["run"]["id"]

            self.assertRegex(scene_id, r"^scene-[A-Za-z0-9._-]+$")
            self.assertRegex(run_id, r"^run-[A-Za-z0-9._-]+$")
            self.assertTrue((root / scene_id / run_id / "manifest.json").is_file())
            self.assertFalse((root.parent / "Garden").exists())

    def test_create_scene_rejects_unsafe_camera_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SpatialStoreError):
                SpatialStore(Path(directory)).create_scene(
                    "Garden", ["tapo-front", "../../elsewhere"]
                )

    def test_artifacts_must_be_declared_by_the_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root)
            store = SpatialStore(root)

            path, media_type = store.artifact("garden", "preview", "point_cloud")
            self.assertEqual(path.name, "cloud.ply")
            self.assertEqual(media_type, "application/octet-stream")
            with self.assertRaises(SpatialStoreError):
                store.artifact("garden", "preview", "secret")

    def test_traversal_identifiers_are_rejected(self):
        store = SpatialStore("unused")
        for values in (("../garden", "run", "cloud"), ("garden", "..", "cloud")):
            with self.subTest(values=values):
                with self.assertRaises(SpatialStoreError):
                    store.artifact(*values)

    def test_invalid_manifests_do_not_break_scene_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root)
            broken = root / "broken" / "run"
            broken.mkdir(parents=True)
            (broken / "manifest.json").write_text("not json", encoding="utf-8")

            self.assertEqual(len(SpatialStore(root).list_scenes()), 1)


if __name__ == "__main__":
    unittest.main()
