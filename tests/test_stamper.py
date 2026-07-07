from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard.stamper import (
    _ClockEpochDiscontinuity,
    _MediaTimeline,
    _InputMetadataNotReady,
    _StampAnchor,
    _StamperSupervisor,
    _StamperWorker,
)
from wanyard.db import SourceDB


class StamperClockTimelineTests(unittest.TestCase):
    def test_media_pts_are_derived_from_clock(self) -> None:
        timeline = _MediaTimeline(max_gap_seconds=2.0)

        self.assertEqual(timeline.pts(1_000.0), 0)
        self.assertEqual(timeline.pts(1_000.1), 9_000)
        self.assertEqual(timeline.pts(1_000.25), 22_500)

    def test_rtp_jump_reanchors_without_corrupting_media_cadence(self) -> None:
        anchor = _StampAnchor("garden")
        timeline = _MediaTimeline(max_gap_seconds=2.0)

        first = anchor.observe(100.0, 1_000.0)
        second = anchor.observe(100.1, 1_000.1)
        after_forward_jump = anchor.observe(113.5, 1_000.2)
        after_backward_jump = anchor.observe(87.0, 1_000.3)

        self.assertEqual(timeline.pts(first), 0)
        self.assertEqual(timeline.pts(second), 9_000)
        self.assertEqual(timeline.pts(after_forward_jump), 18_000)
        self.assertEqual(timeline.pts(after_backward_jump), 27_000)

    def test_non_increasing_clock_frame_is_discarded(self) -> None:
        timeline = _MediaTimeline(max_gap_seconds=2.0)

        self.assertEqual(timeline.pts(1_000.0), 0)
        self.assertIsNone(timeline.pts(999.9))
        self.assertEqual(timeline.pts(1_000.1), 9_000)

    def test_real_clock_gap_requires_a_new_media_epoch(self) -> None:
        timeline = _MediaTimeline(max_gap_seconds=2.0)

        self.assertEqual(timeline.pts(1_000.0), 0)
        with self.assertRaises(_ClockEpochDiscontinuity) as raised:
            timeline.pts(1_183.0)

        self.assertEqual(raised.exception.gap_seconds, 183.0)

    def test_gap_uses_epoch_rollover_without_reconnect_backoff(self) -> None:
        store = mock.Mock()
        worker = _StamperWorker(
            "mediamtx", "tapo-garden", threading.Event(), health_store=store
        )
        calls = 0

        def stream() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise _ClockEpochDiscontinuity(183.0)
            worker.local_stop.set()

        with (
            mock.patch.object(worker, "_stream", side_effect=stream),
            mock.patch.object(worker, "_wait") as wait,
        ):
            worker.run()

        wait.assert_called_once_with(0.25)
        self.assertEqual(worker._reconnect_count, 0)
        self.assertEqual(worker._fallback_count, 0)
        gap_events = [
            call for call in store.event.call_args_list if call.args[3] == "clock_gap"
        ]
        self.assertEqual(len(gap_events), 1)


class StamperRateControlTests(unittest.TestCase):
    def test_source_h264_limit_overrides_global(self) -> None:
        env = {
            "WANYARD_STAMP_MAXRATE": "2.5M",
            "WANYARD_STAMP_BUFSIZE": "5M",
            "WANYARD_STAMP_TAPO_GARDEN_H264_MAXRATE": "5M",
            "WANYARD_STAMP_TAPO_GARDEN_H264_BUFSIZE": "10M",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            worker = _StamperWorker("mediamtx", "tapo-garden", threading.Event())
            options = worker._video_options("libx264", 15)

        self.assertEqual(options["maxrate"], "5M")
        self.assertEqual(options["bufsize"], "10M")

    def test_other_sources_keep_global_limit(self) -> None:
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


class StamperCodecTests(unittest.TestCase):
    def test_fallback_encoder_is_h264_only(self) -> None:
        """The re-encode fallback carries the clock as an H.264 SEI NAL, so
        hevc requests resolve to h264."""
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("wanyard.stamper._nvenc_available",
                            return_value=True):
                for requested in ("hevc", "hevc_nvenc", "h265_nvenc", "auto"):
                    worker = _StamperWorker(
                        "mediamtx", "cam", threading.Event())
                    worker.encoder = requested
                    self.assertIn(worker._video_codec(),
                                  ("h264_nvenc", "libx264"))
            with mock.patch("wanyard.stamper._nvenc_available",
                            return_value=False):
                worker = _StamperWorker("mediamtx", "cam", threading.Event())
                worker.encoder = "auto"
                self.assertEqual(worker._video_codec(), "libx264")

    def test_demotion_is_nvenc_to_libx264(self) -> None:
        worker = _StamperWorker("mediamtx", "cam", threading.Event())
        worker._active_codec = "h264_nvenc"
        self.assertTrue(worker._demote_on_encoder_error(
            RuntimeError("avcodec_open2 failed")))
        self.assertEqual(worker._codec_override, "libx264")


class StamperReconnectMetadataTests(unittest.TestCase):
    def test_zero_dimensions_retry_without_encoder_demotion(self) -> None:
        store = mock.Mock()
        worker = _StamperWorker(
            "mediamtx", "tapo-garden", threading.Event(), health_store=store
        )
        worker._active_codec = "h264_nvenc"
        stream = SimpleNamespace(
            type="video",
            codec_context=SimpleNamespace(width=0, height=0),
            average_rate=15,
        )
        inp = SimpleNamespace(streams=[stream], close=mock.Mock())
        av_module = SimpleNamespace(open=mock.Mock(return_value=inp))

        with mock.patch.dict(sys.modules, {"av": av_module}):
            with self.assertRaisesRegex(_InputMetadataNotReady, "0x0") as raised:
                worker._stream()

        av_module.open.assert_called_once()
        inp.close.assert_called_once()
        self.assertFalse(worker._demote_on_encoder_error(raised.exception))
        self.assertIsNone(worker._codec_override)
        self.assertEqual(worker._fallback_count, 0)
        store.event.assert_called_once()
        self.assertEqual(store.event.call_args.args[3], "input_metadata_invalid")

    def test_valid_dimensions_are_used_before_encoder_open(self) -> None:
        worker = _StamperWorker("mediamtx", "tapo-garden", threading.Event())
        stream = SimpleNamespace(
            codec_context=SimpleNamespace(width=2560, height=1440),
            average_rate=15,
        )

        self.assertEqual(worker._video_metadata(stream), (2560, 1440, 15.0))
        self.assertIsNone(worker._codec_override)


if __name__ == "__main__":
    unittest.main()
