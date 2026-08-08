from __future__ import annotations

import asyncio
import json
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


def _request(
    source_id: str,
    payload: object | None = None,
    *,
    raw_body: bytes | None = None,
) -> Request:
    body = raw_body if raw_body is not None else json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    path = f"/api/sources/{source_id}"
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "PATCH",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "path_params": {"source_id": source_id},
        "client": ("test", 1),
        "server": ("test", 80),
    }, receive)


class SourceRenameTests(unittest.TestCase):
    def _fixture(self, directory: str):
        root = Path(directory)
        source_db = SourceDB(root / "sources.db")
        source_db.insert(RtspSourceRow(
            id="garden-old",
            name="Garden old",
            url="rtsp://camera/garden",
            interval_seconds=60,
            enabled=True,
            rtsp_transport="tcp",
            timeout_seconds=10,
            output_subdir="garden-old",
        ))
        video_dir = root / "video"
        recording = video_dir / "garden-old" / "clip.mp4"
        recording.parent.mkdir(parents=True)
        recording.write_bytes(b"recording")
        capture = mock.Mock()
        app = make_app(
            AppConfig(),
            source_db=source_db,
            video_dir=video_dir,
            video_db=VideoSegmentDB(root / "video.db"),
            capture_worker=capture,
        )
        route = next(
            route for route in app.app.routes
            if getattr(route, "path", None) == "/api/sources/{source_id}"
        )
        return source_db, recording, capture, route

    def test_rename_changes_only_the_display_name(self):
        with tempfile.TemporaryDirectory() as directory:
            source_db, recording, capture, route = self._fixture(directory)
            response = asyncio.run(route.endpoint(_request(
                "garden-old", {"name": "Back garden"}
            )))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(response.body)["source"], {
                "id": "garden-old", "name": "Back garden"
            })
            row = source_db.list()[0]
            self.assertEqual(row.id, "garden-old")
            self.assertEqual(row.name, "Back garden")
            self.assertEqual(row.url, "rtsp://camera/garden")
            self.assertEqual(row.output_subdir, "garden-old")
            self.assertTrue(recording.exists())
            capture.assert_not_called()

    def test_rename_trims_the_saved_name(self):
        with tempfile.TemporaryDirectory() as directory:
            source_db, _recording, _capture, route = self._fixture(directory)
            response = asyncio.run(route.endpoint(_request(
                "garden-old", {"name": "  Garden left  "}
            )))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(source_db.list()[0].name, "Garden left")

    def test_rename_validates_name_and_unknown_camera(self):
        invalid_names = [None, "", "   ", "x" * 81, "bad\nname"]
        with tempfile.TemporaryDirectory() as directory:
            source_db, _recording, _capture, route = self._fixture(directory)
            for name in invalid_names:
                with self.subTest(name=name):
                    response = asyncio.run(route.endpoint(_request(
                        "garden-old", {"name": name}
                    )))
                    self.assertEqual(response.status_code, 400)
            response = asyncio.run(route.endpoint(_request(
                "missing", {"name": "Anything"}
            )))
            self.assertEqual(response.status_code, 404)
            self.assertEqual(source_db.list()[0].name, "Garden old")

    def test_rename_rejects_invalid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            _source_db, _recording, _capture, route = self._fixture(directory)
            response = asyncio.run(route.endpoint(_request(
                "garden-old", raw_body=b"not-json"
            )))
            self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
