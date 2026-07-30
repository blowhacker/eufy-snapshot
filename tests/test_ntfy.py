from __future__ import annotations

import json
import asyncio
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

from wanyard.ntfy import (  # noqa: E402
    NtfyPublishError,
    dispatch_ntfy_notifications,
    load_ntfy_config,
    publish_ntfy,
    save_ntfy_config,
)
from wanyard.video import VideoSegmentDB  # noqa: E402
from wanyard.config import AppConfig  # noqa: E402
from wanyard.web import make_app  # noqa: E402


class _Response:
    status = 200

    def __init__(self, body: bytes = b'{"id":"remote-1"}') -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


def _insert_notification(db: VideoSegmentDB, event_ts: float) -> int:
    with db._connect() as conn:
        rule_id = conn.execute(
            "INSERT INTO notification_rules(name, source_id, created_at)"
            " VALUES('Garden person','front',?)",
            (event_ts - 1,),
        ).lastrowid
        return int(conn.execute(
            "INSERT INTO notification_events"
            " (rule_id, rule_name, source_id, zone_ref, event_ref, event_ts,"
            " class, confidence, title, body, thumb_url, target_url, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rule_id,
                "Garden person",
                "front",
                "whole_frame",
                f"d:{int(event_ts * 10)}",
                event_ts,
                "person",
                0.91,
                "Person detected",
                "Garden person · Whole frame",
                f"/api/video/event-thumb/d:{int(event_ts * 10)}",
                f"/?source=front&ts={event_ts:.3f}&cls=person&zone=none",
                event_ts,
            ),
        ).lastrowid)


class NtfyConfigTests(unittest.TestCase):
    def test_default_topic_is_unique_looking_editable_and_thumbnail_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = VideoSegmentDB(Path(tmp) / "video.db")
            config = load_ntfy_config(
                db, default_base_url="https://camera.example"
            )

            self.assertRegex(config.topic, r"^wanyard-[0-9a-f]{24}$")
            self.assertFalse(config.enabled)
            self.assertTrue(config.include_thumbnail)
            self.assertEqual(config.base_url, "https://camera.example")

            edited = save_ntfy_config(db, {
                "topic": "my_editable_topic-2",
                "server": "https://ntfy.example",
                "base_url": "https://camera.example:8091",
                "include_thumbnail": False,
                "enabled": True,
            })
            self.assertEqual(edited.topic, "my_editable_topic-2")
            self.assertFalse(edited.include_thumbnail)

    def test_rejects_invalid_topic_and_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = VideoSegmentDB(Path(tmp) / "video.db")
            with self.assertRaisesRegex(ValueError, "topic"):
                save_ntfy_config(db, {"topic": "not a topic"})
            with self.assertRaisesRegex(ValueError, "http or https"):
                save_ntfy_config(db, {
                    "topic": "valid-topic",
                    "server": "ftp://example.com",
                })

    def test_public_payload_redacts_saved_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = VideoSegmentDB(Path(tmp) / "video.db")
            config = save_ntfy_config(db, {
                "topic": "valid-topic",
                "server": "https://ntfy.sh",
                "token": "tk_secret",
            })

            payload = config.public_payload()

            self.assertTrue(payload["token_set"])
            self.assertNotIn("token", payload)
            self.assertNotIn("tk_secret", json.dumps(payload))


class NtfyPublishTests(unittest.TestCase):
    def test_json_publish_includes_click_thumbnail_and_bearer_token(self) -> None:
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response()

        with tempfile.TemporaryDirectory() as tmp:
            db = VideoSegmentDB(Path(tmp) / "video.db")
            config = save_ntfy_config(db, {
                "topic": "wanyard-private",
                "server": "https://ntfy.example",
                "token": "tk_secret",
                "base_url": "https://camera.example:8091",
                "include_thumbnail": True,
            })
            remote_id = publish_ntfy(config, {
                "title": "Bird detected",
                "body": "Peanut table · Garden",
                "class": "bird",
                "event_ts": 1234.5,
                "target_url": "/?source=garden&ts=1234.5",
                "thumb_url": "/api/video/event-thumb/d:7",
            }, opener=opener)

        request = captured["request"]
        payload = json.loads(request.data)
        self.assertEqual(remote_id, "remote-1")
        self.assertEqual(request.full_url, "https://ntfy.example/")
        self.assertEqual(
            request.get_header("Authorization"), "Bearer tk_secret"
        )
        self.assertEqual(payload["topic"], "wanyard-private")
        self.assertEqual(
            payload["click"],
            "https://camera.example:8091/?source=garden&ts=1234.5",
        )
        self.assertEqual(
            payload["attach"],
            "https://camera.example:8091/api/video/event-thumb/d:7",
        )
        self.assertEqual(payload["tags"], ["bird"])

    def test_thumbnail_toggle_removes_attachment(self) -> None:
        captured = {}

        def opener(request, timeout):
            captured["payload"] = json.loads(request.data)
            return _Response()

        with tempfile.TemporaryDirectory() as tmp:
            db = VideoSegmentDB(Path(tmp) / "video.db")
            config = save_ntfy_config(db, {
                "topic": "wanyard-private",
                "server": "https://ntfy.sh",
                "base_url": "https://camera.example",
                "include_thumbnail": False,
            })
            publish_ntfy(config, {
                "title": "Dog detected",
                "body": "Garden",
                "class": "dog",
                "thumb_url": "/thumb.jpg",
            }, opener=opener)

        self.assertNotIn("attach", captured["payload"])
        self.assertNotIn("filename", captured["payload"])


class NtfyDeliveryTests(unittest.TestCase):
    def test_enabling_seeds_past_notifications_and_delivers_only_new(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            db = VideoSegmentDB(Path(tmp) / "video.db")
            old_id = _insert_notification(db, 1000.0)
            config = save_ntfy_config(db, {
                "topic": "wanyard-private",
                "server": "https://ntfy.sh",
                "base_url": "https://camera.example",
                "enabled": True,
            })
            new_id = _insert_notification(db, 1002.0)

            def publisher(_config, notification):
                calls.append(notification["id"])
                return "remote"

            result = dispatch_ntfy_notifications(
                db, now=1003.0, publisher=publisher
            )

            self.assertNotEqual(old_id, new_id)
            self.assertEqual(calls, [new_id])
            self.assertEqual(result["delivered"], 1)
            status = db.notification_delivery_status(
                "ntfy", config.destination_key
            )
            self.assertEqual(status["counts"], {"delivered": 1})

    def test_reenable_does_not_replay_notifications_from_paused_period(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            db = VideoSegmentDB(Path(tmp) / "video.db")
            save_ntfy_config(db, {
                "topic": "wanyard-private",
                "server": "https://ntfy.sh",
                "enabled": False,
            })
            _insert_notification(db, 1000.0)
            save_ntfy_config(db, {
                "topic": "wanyard-private",
                "server": "https://ntfy.sh",
                "enabled": True,
            })

            dispatch_ntfy_notifications(
                db,
                now=1001.0,
                publisher=lambda _config, notification: calls.append(
                    notification["id"]
                ),
            )

        self.assertEqual(calls, [])

    def test_transient_failure_retries_without_duplicate_completed_delivery(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            db = VideoSegmentDB(Path(tmp) / "video.db")
            config = save_ntfy_config(db, {
                "topic": "wanyard-private",
                "server": "https://ntfy.sh",
                "enabled": True,
            })
            notification_id = _insert_notification(db, 1000.0)

            def failing(_config, notification):
                calls.append(("fail", notification["id"]))
                raise NtfyPublishError("temporary", status_code=503)

            first = dispatch_ntfy_notifications(
                db, now=1001.0, publisher=failing
            )
            second = dispatch_ntfy_notifications(
                db,
                now=1006.0,
                publisher=lambda _config, notification: (
                    calls.append(("ok", notification["id"])) or "remote"
                ),
            )
            third = dispatch_ntfy_notifications(
                db,
                now=1012.0,
                publisher=lambda _config, notification: calls.append(
                    ("duplicate", notification["id"])
                ),
            )

            self.assertEqual(first["failed"], 1)
            self.assertEqual(second["delivered"], 1)
            self.assertEqual(third["delivered"], 0)
            self.assertEqual(
                calls, [("fail", notification_id), ("ok", notification_id)]
            )
            status = db.notification_delivery_status(
                "ntfy", config.destination_key
            )
            self.assertEqual(status["counts"], {"delivered": 1})


class NtfySettingsApiTests(unittest.TestCase):
    @staticmethod
    def _request(path: str, method: str = "GET", body: dict | None = None) -> Request:
        raw = json.dumps(body or {}).encode()

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        return Request({
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"camera.example")],
            "client": ("test", 1),
            "server": ("camera.example", 443),
        }, receive)

    def test_settings_api_generates_topic_and_test_uses_submitted_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = VideoSegmentDB(Path(tmp) / "video.db")
            app = make_app(
                AppConfig(),
                video_dir=Path(tmp) / "video",
                video_db=db,
            )
            routes = {
                route.path: route.endpoint
                for route in app.app.routes
                if hasattr(route, "path") and hasattr(route, "endpoint")
            }
            response = asyncio.run(routes["/api/settings/ntfy"](
                self._request("/api/settings/ntfy")
            ))
            initial = json.loads(response.body)

            with mock.patch(
                "wanyard.web.send_ntfy_test", return_value="remote-test"
            ) as send:
                response = asyncio.run(routes["/api/settings/ntfy/test"](
                    self._request(
                        "/api/settings/ntfy/test",
                        "POST",
                        {
                            "enabled": True,
                            "topic": initial["topic"],
                            "server": "https://ntfy.sh",
                            "base_url": "https://camera.example",
                            "include_thumbnail": True,
                        },
                    )
                ))

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["remote_id"], "remote-test")
        self.assertTrue(payload["settings"]["enabled"])
        self.assertTrue(payload["settings"]["include_thumbnail"])
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
