from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard.stamper import _StamperWorker


class StamperRateControlTests(unittest.TestCase):
    def test_source_h264_limit_does_not_raise_hevc_limit(self) -> None:
        env = {
            "WANYARD_STAMP_MAXRATE": "2.5M",
            "WANYARD_STAMP_BUFSIZE": "5M",
            "WANYARD_STAMP_TAPO_GARDEN_H264_MAXRATE": "5M",
            "WANYARD_STAMP_TAPO_GARDEN_H264_BUFSIZE": "10M",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            worker = _StamperWorker("mediamtx", "tapo-garden", threading.Event())
            h264 = worker._video_options("libx264", 15)
            hevc = worker._video_options("hevc_nvenc", 15)

        self.assertEqual(h264["maxrate"], "5M")
        self.assertEqual(h264["bufsize"], "10M")
        self.assertEqual(hevc["maxrate"], "2.5M")
        self.assertEqual(hevc["bufsize"], "5M")

    def test_other_sources_keep_global_h264_limit(self) -> None:
        env = {
            "WANYARD_STAMP_MAXRATE": "2.5M",
            "WANYARD_STAMP_BUFSIZE": "5M",
            "WANYARD_STAMP_TAPO_GARDEN_H264_MAXRATE": "5M",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            worker = _StamperWorker("mediamtx", "tapo-front", threading.Event())
            options = worker._video_options("libx264", 20)

        self.assertEqual(options["maxrate"], "2.5M")
        self.assertEqual(options["bufsize"], "5M")


if __name__ == "__main__":
    unittest.main()
