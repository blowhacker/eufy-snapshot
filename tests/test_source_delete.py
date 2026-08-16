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
from wanyard.web import _source_storage_sizes, make_app


def _request(path: str, body: bytes = b"") -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "DELETE",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "path_params": {"source_id": "desk"},
        "client": ("test", 1),
        "server": ("test", 80),
    }, receive)


class SourceDeleteTests(unittest.TestCase):
    def test_storage_sizes_include_derived_artifacts_and_skip_empty_dirs(self):
        with tempfile.TemporaryDirectory() as directory:
            video_dir = Path(directory)
            cache = video_dir / "desk" / "2026" / ".thumbcache"
            cache.mkdir(parents=True)
            (cache / "crop.jpg").write_bytes(b"thumbnail")
            (video_dir / "empty-camera").mkdir()
            live = video_dir / "live" / "desk"
            live.mkdir(parents=True)
            (live / "index.m3u8").write_bytes(b"playlist")

            self.assertEqual(_source_storage_sizes(video_dir), {"desk": 9})

    def _fixture(self, directory: str):
        root = Path(directory)
        source_db = SourceDB(root / "sources.db")
        source_db.insert(RtspSourceRow(
            id="desk", name="Desk", url="rtsp://camera/desk",
            interval_seconds=60, enabled=True, rtsp_transport="tcp",
            timeout_seconds=10, output_subdir="desk",
        ))
        video_dir = root / "video"
        media = video_dir / "desk" / "clip.mp4"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"recording")
        video_db = VideoSegmentDB(root / "video.db")
        segment_id = video_db.open_segment("desk", "desk/clip.mp4", 100.0)
        video_db.close_segment(segment_id, 110.0, None, None)
        capture = mock.Mock()
        app = make_app(
            AppConfig(), source_db=source_db, video_dir=video_dir,
            video_db=video_db, capture_worker=capture,
        )
        route = next(
            route for route in app.app.routes
            if getattr(route, "path", None) == "/api/sources/{source_id}"
        )
        return source_db, media, capture, route

    def test_default_delete_retains_recordings(self):
        with tempfile.TemporaryDirectory() as directory:
            source_db, media, capture, route = self._fixture(directory)
            with mock.patch("wanyard.web.native_hls.unregister_source_runtime"):
                response = asyncio.run(route.endpoint(_request("/api/sources/desk")))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(response.body)["recordings"], "retained")
            self.assertNotIn("desk", source_db.ids())
            self.assertTrue(media.exists())
            capture.remove_source.assert_called_once_with("desk")

    def test_requested_delete_purges_only_source_recordings(self):
        with tempfile.TemporaryDirectory() as directory:
            source_db, media, capture, route = self._fixture(directory)
            body = json.dumps({"delete_recordings": True}).encode()
            with mock.patch("wanyard.web.native_hls.unregister_source_runtime"):
                response = asyncio.run(
                    route.endpoint(_request("/api/sources/desk", body))
                )

            payload = json.loads(response.body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(payload["recordings"], "deleted")
            self.assertEqual(payload["cleanup"]["deleted_segments"], 1)
            self.assertNotIn("desk", source_db.ids())
            self.assertFalse(media.exists())
            capture.remove_source.assert_called_once_with("desk")


if __name__ == "__main__":
    unittest.main()
