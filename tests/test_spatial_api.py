from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard.config import AppConfig
from wanyard.db import RtspSourceRow, SourceDB
from wanyard.spatial import SpatialStore
from wanyard.video import VideoSegmentDB
from wanyard.web import make_app


def _request(path: str, payload: dict | None = None, *, method="POST", path_params=None) -> Request:
    body = json.dumps(payload).encode() if payload is not None else b""
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http", "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": [(b"content-type", b"application/json")],
        "client": ("test", 1), "server": ("test", 80),
        "path_params": path_params or {},
    }, receive)


class SpatialApiTests(unittest.TestCase):
    def _app(self, directory: str):
        root = Path(directory)
        source_db = SourceDB(root / "sources.db")
        for source_id in ("front", "garden"):
            source_db.insert(RtspSourceRow(
                id=source_id, name=source_id.title(),
                url=f"rtsp://camera/{source_id}", interval_seconds=60,
                enabled=True, rtsp_transport="tcp", timeout_seconds=10,
                output_subdir=source_id,
            ))
        video_dir = root / "video"
        video_dir.mkdir()
        video_db = VideoSegmentDB(video_dir / "video.db")
        with mock.patch.dict(os.environ, {
            "WANYARD_SPATIAL_DIR": str(root / "spatial")
        }):
            return make_app(
                AppConfig(), source_db=source_db, video_dir=video_dir,
                video_db=video_db,
            )

    @staticmethod
    def _route(app, path: str):
        return next(
            route for route in app.app.routes
            if getattr(route, "path", None) == path
        )

    def test_connected_check_authorizes_queued_scene_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(directory)
            check_route = self._route(app, "/api/spatial/feasibility")
            scene_route = self._route(app, "/api/spatial/scenes")
            report = {
                "camera_ids": ["front", "garden"],
                "mergeable": True,
                "status": "mergeable",
                "pairs": [{
                    "left_camera_id": "front", "right_camera_id": "garden",
                    "status": "promising",
                }],
                "components": [["front", "garden"]],
                "checked_at": 100.0,
            }
            with mock.patch("wanyard.web.inspect_camera_set", return_value=report):
                checked = asyncio.run(check_route.endpoint(_request(
                    "/api/spatial/feasibility",
                    {"camera_ids": ["front", "garden"]},
                )))
            checked_payload = json.loads(checked.body)["feasibility"]

            created = asyncio.run(scene_route.endpoint(_request(
                "/api/spatial/scenes",
                {
                    "name": "Front garden",
                    "camera_ids": ["front", "garden"],
                    "feasibility_id": checked_payload["id"],
                },
            )))
            payload = json.loads(created.body)

            self.assertEqual(checked.status_code, 200)
            self.assertEqual(created.status_code, 202)
            self.assertEqual(payload["scene"]["runs"][0]["status"], "queued")
            self.assertEqual(
                payload["scene"]["camera_ids"], ["front", "garden"]
            )

    def test_scene_creation_rejects_changed_camera_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(directory)
            check_route = self._route(app, "/api/spatial/feasibility")
            scene_route = self._route(app, "/api/spatial/scenes")
            report = {
                "camera_ids": ["front", "garden"], "mergeable": True,
                "status": "mergeable", "pairs": [],
                "components": [["front", "garden"]], "checked_at": 100.0,
            }
            with mock.patch("wanyard.web.inspect_camera_set", return_value=report):
                checked = asyncio.run(check_route.endpoint(_request(
                    "/api/spatial/feasibility",
                    {"camera_ids": ["front", "garden"]},
                )))
            check_id = json.loads(checked.body)["feasibility"]["id"]

            response = asyncio.run(scene_route.endpoint(_request(
                "/api/spatial/scenes",
                {"name": "Changed", "camera_ids": ["garden", "front"],
                 "feasibility_id": check_id},
            )))

            self.assertEqual(response.status_code, 409)
            self.assertIn("changed", json.loads(response.body)["error"])

    def test_overlap_check_rejects_unknown_camera(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(directory)
            route = self._route(app, "/api/spatial/feasibility")
            response = asyncio.run(route.endpoint(_request(
                "/api/spatial/feasibility",
                {"camera_ids": ["front", "missing"]},
            )))
            self.assertEqual(response.status_code, 404)

    def test_remove_scene_archives_it_from_the_index(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(directory)
            check_route = self._route(app, "/api/spatial/feasibility")
            scenes_route = self._route(app, "/api/spatial/scenes")
            remove_route = self._route(app, "/api/spatial/scenes/{scene_id}")
            report = {
                "camera_ids": ["front", "garden"], "mergeable": True,
                "status": "mergeable", "pairs": [],
                "components": [["front", "garden"]], "checked_at": 100.0,
            }
            with mock.patch("wanyard.web.inspect_camera_set", return_value=report):
                checked = asyncio.run(check_route.endpoint(_request(
                    "/api/spatial/feasibility",
                    {"camera_ids": ["front", "garden"]},
                )))
            check_id = json.loads(checked.body)["feasibility"]["id"]
            created = asyncio.run(scenes_route.endpoint(_request(
                "/api/spatial/scenes",
                {"name": "Temporary", "camera_ids": ["front", "garden"],
                 "feasibility_id": check_id},
            )))
            scene_id = json.loads(created.body)["scene_id"]

            removed = asyncio.run(remove_route.endpoint(_request(
                f"/api/spatial/scenes/{scene_id}", None, method="DELETE",
                path_params={"scene_id": scene_id},
            )))
            listed = asyncio.run(scenes_route.endpoint(_request(
                "/api/spatial/scenes", None, method="GET"
            )))

            self.assertEqual(removed.status_code, 200)
            self.assertTrue(json.loads(removed.body)["recoverable"])
            self.assertEqual(json.loads(listed.body)["scenes"], [])

    def test_geometry_refresh_queues_one_idempotent_replacement_run(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(directory)
            store = SpatialStore(Path(directory) / "spatial")
            initial = store.create_scene("Front", ["front", "garden"])
            scene_id = initial["scene"]["id"]
            store.update_run(
                scene_id, initial["run"]["id"], status="ready", artifacts={}
            )
            route = self._route(app, "/api/spatial/scenes/{scene_id}/runs")
            request = lambda: _request(
                f"/api/spatial/scenes/{scene_id}/runs", None,
                path_params={"scene_id": scene_id},
            )

            queued = asyncio.run(route.endpoint(request()))
            duplicate = asyncio.run(route.endpoint(request()))
            queued_payload = json.loads(queued.body)
            duplicate_payload = json.loads(duplicate.body)

            self.assertEqual(queued.status_code, 202)
            self.assertTrue(queued_payload["queued"])
            self.assertEqual(duplicate.status_code, 200)
            self.assertFalse(duplicate_payload["queued"])
            self.assertEqual(queued_payload["run_id"], duplicate_payload["run_id"])
            self.assertEqual(
                [run["status"] for run in store.list_scenes()[0]["runs"]],
                ["queued", "ready"],
            )


if __name__ == "__main__":
    unittest.main()
