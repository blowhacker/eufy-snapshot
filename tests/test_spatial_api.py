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
from wanyard.video import VideoSegmentDB
from wanyard.web import make_app


def _request(path: str, payload: dict) -> Request:
    body = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http", "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": [(b"content-type", b"application/json")],
        "client": ("test", 1), "server": ("test", 80),
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


if __name__ == "__main__":
    unittest.main()
