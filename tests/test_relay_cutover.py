from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard.capture import resolve_rtsp_url
from wanyard import cli
from wanyard.config import AppConfig, SourceConfig
from wanyard.live_detector import _target_relay_paths
from wanyard import native_hls


class RelayCutoverTests(unittest.TestCase):
    def test_resolve_rtsp_url_applies_relay_path_suffix(self) -> None:
        source = SourceConfig(id="tapo-front", name="Front", url="rtsp://camera/stream")
        with mock.patch.dict(
            os.environ,
            {
                "WANYARD_RELAY_HOST": "mediamtx",
                "WANYARD_RELAY_PATH_SUFFIX": "-stamped",
            },
            clear=False,
        ):
            self.assertEqual(
                resolve_rtsp_url(source),
                "rtsp://mediamtx:8554/tapo-front-stamped",
            )
            self.assertEqual(resolve_rtsp_url(source, direct=True), "rtsp://camera/stream")

    def test_live_detector_maps_stamped_paths_to_source_ids(self) -> None:
        self.assertEqual(
            _target_relay_paths(
                ["tapo-front", "tapo-front-stamped", "tapo-garden-stamped"],
                "-stamped",
            ),
            {
                "tapo-front": "tapo-front-stamped",
                "tapo-garden": "tapo-garden-stamped",
            },
        )

    def test_live_detector_default_ignores_shadow_stamped_paths(self) -> None:
        self.assertEqual(
            _target_relay_paths(
                ["tapo-front", "tapo-front-stamped", "tapo-garden"],
                "",
            ),
            {
                "tapo-front": "tapo-front",
                "tapo-garden": "tapo-garden",
            },
        )

    def test_native_hls_url_uses_stamped_relay_path(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "WANYARD_RELAY_HOST": "mediamtx",
                "WANYARD_MEDIAMTX_HLS_PORT": "8891",
                "WANYARD_RELAY_PATH_SUFFIX": "-stamped",
            },
            clear=True,
        ):
            self.assertEqual(
                native_hls.source_path("tapo-front"),
                "tapo-front-stamped",
            )
            self.assertEqual(
                native_hls.public_manifest_url("tapo-front"),
                "/video/native-live/tapo-front-stamped/index.m3u8",
            )
            self.assertEqual(
                native_hls.upstream_url(
                    "tapo-front-stamped",
                    "index.m3u8",
                    "_HLS_msn=7&_HLS_part=3",
                ),
                "http://mediamtx:8891/tapo-front-stamped/index.m3u8?_HLS_msn=7&_HLS_part=3",
            )

    def test_native_hls_rejects_unsafe_proxy_paths(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "WANYARD_RELAY_HOST": "mediamtx",
                "WANYARD_MEDIAMTX_HLS_PORT": "8891",
            },
            clear=True,
        ):
            self.assertIsNone(
                native_hls.upstream_url("../tapo-front", "video1_stream.m3u8")
            )
            self.assertIsNone(
                native_hls.upstream_url("tapo-front", "../video1_stream.m3u8")
            )

    def test_native_hls_port_invalid_values_fall_back_to_default(self) -> None:
        for raw in ("", "bad", "0", "70000"):
            with self.subTest(raw=raw):
                with mock.patch.dict(
                    os.environ,
                    {"WANYARD_MEDIAMTX_HLS_PORT": raw},
                    clear=True,
                ):
                    self.assertEqual(native_hls.hls_port(), native_hls.DEFAULT_PORT)

    def test_gen_go2rtc_pins_webrtc_candidate_to_media_port(self) -> None:
        db = mock.Mock()
        db.to_source_configs.return_value = [
            SourceConfig(
                id="tapo-front",
                name="Front",
                url="rtsp://camera/stream1",
            )
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "go2rtc.yaml"
            args = mock.Mock(out=str(out))
            with mock.patch("wanyard.cli.SourceDB", return_value=db), mock.patch.dict(
                os.environ,
                {"WANYARD_GO2RTC_WEBRTC_PORT": "8557"},
                clear=True,
            ):
                self.assertEqual(
                    cli.cmd_gen_go2rtc(args, AppConfig(db_path=Path("sources.db"))),
                    0,
                )

            text = out.read_text(encoding="utf-8")
            self.assertIn('  listen: ":8557"', text)
            self.assertIn('    - "stun:8557"', text)
            self.assertIn("  tapo-front: rtsp://camera/stream1", text)


if __name__ == "__main__":
    unittest.main()
