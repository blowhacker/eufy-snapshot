from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard.capture import resolve_rtsp_url
from wanyard.config import SourceConfig
from wanyard.live_detector import _target_relay_paths


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


if __name__ == "__main__":
    unittest.main()
