from __future__ import annotations

import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard import bitc


class BitcCodecTests(unittest.TestCase):
    def test_round_trip_random_values(self) -> None:
        rng = random.Random(0xB17C)
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        for _ in range(1000):
            unix_seconds = rng.uniform(1_700_000_000.0, 2_700_000_000.0)
            value = bitc.encode_value(unix_seconds)
            marked = frame.copy()
            returned = bitc.render(marked, value)
            self.assertIs(returned, marked)
            decoded, crc_ok = bitc.decode_value(marked)
            decoded_seconds, seconds_crc_ok = bitc.decode(marked)
            self.assertTrue(crc_ok)
            self.assertTrue(seconds_crc_ok)
            self.assertEqual(decoded, value)
            self.assertEqual(round(decoded_seconds * 100), value)

    def test_mask_zeroes_exact_strip(self) -> None:
        frame = np.full((48, bitc.WIDTH + 32, 3), 213, dtype=np.uint8)
        original = frame.copy()
        returned = bitc.mask(frame)
        self.assertIs(returned, frame)

        x0, y0, width, height = bitc.geometry(frame)
        expected = original.copy()
        expected[y0 : y0 + height, x0 : x0 + width, :] = 0
        np.testing.assert_array_equal(frame, expected)

    def test_crc_failure_returns_none_false(self) -> None:
        frame = np.zeros((64, bitc.WIDTH, 3), dtype=np.uint8)
        value = bitc.encode_value(1_781_234_567.89)
        bitc.render(frame, value)
        self.assertEqual(bitc.decode_value(frame), (value, True))

        x0, y0, _, _ = bitc.geometry(frame)
        pad = bitc.CELL // 4
        replacement = 0 if value & 1 else 255
        frame[
            y0 + pad : y0 + bitc.CELL - pad,
            x0 + pad : x0 + bitc.CELL - pad,
            :,
        ] = replacement
        self.assertEqual(bitc.decode_value(frame), (None, False))
        self.assertEqual(bitc.decode(frame), (None, False))


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for the lossy BITC test")
class BitcLossyH264Tests(unittest.TestCase):
    FRAME_WIDTH = 2304
    FRAME_HEIGHT = 1296
    FRAME_COUNT = 200
    FPS = 15

    @classmethod
    def setUpClass(cls) -> None:
        cls.base_frame = cls._load_feed_frame()

    @classmethod
    def _load_feed_frame(cls) -> np.ndarray:
        feed = ROOT / "site" / "assets" / "feed.jpg"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(feed),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "pipe:1",
        ]
        raw = subprocess.check_output(cmd)
        expected = cls.FRAME_WIDTH * cls.FRAME_HEIGHT * 3
        if len(raw) != expected:
            raise AssertionError(f"decoded feed frame size {len(raw)} != {expected}")
        return np.frombuffer(raw, dtype=np.uint8).reshape(
            (cls.FRAME_HEIGHT, cls.FRAME_WIDTH, 3)
        ).copy()

    def test_lossy_h264_survives_crf_18_23_30(self) -> None:
        results: dict[int, int] = {}
        for crf in (18, 23, 30):
            with self.subTest(crf=crf):
                passed = self._encode_decode_and_count(crf)
                results[crf] = passed
                self.assertEqual(passed, self.FRAME_COUNT)
        print(
            "BITC lossy pass rates: "
            + ", ".join(
                f"crf{crf}={passed}/{self.FRAME_COUNT}" for crf, passed in results.items()
            )
        )

    def _encode_decode_and_count(self, crf: int) -> int:
        with tempfile.TemporaryDirectory(prefix="wanyard-bitc-") as tmp:
            mp4 = Path(tmp) / f"bitc-crf{crf}.mp4"
            self._write_encoded_mp4(mp4, crf)
            return self._count_decoded_markers(mp4)

    def _write_encoded_mp4(self, mp4: Path, crf: int) -> None:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pixel_format",
            "bgr24",
            "-video_size",
            f"{self.FRAME_WIDTH}x{self.FRAME_HEIGHT}",
            "-framerate",
            str(self.FPS),
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            str(mp4),
        ]
        with subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            assert proc.stdin is not None
            try:
                for offset in range(self.FRAME_COUNT):
                    frame = self.base_frame.copy()
                    value = bitc.encode_value(1_781_234_000.0 + offset / 100.0)
                    bitc.render(frame, value)
                    proc.stdin.write(memoryview(frame))
            except BrokenPipeError:
                pass
            finally:
                proc.stdin.close()
            stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            rc = proc.wait()
        if rc != 0:
            raise AssertionError(f"ffmpeg encode failed rc={rc}: {stderr}")

    def _count_decoded_markers(self, mp4: Path) -> int:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(mp4),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "pipe:1",
        ]
        frame_size = self.FRAME_WIDTH * self.FRAME_HEIGHT * 3
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            assert proc.stdout is not None

            passed = 0
            for offset in range(self.FRAME_COUNT):
                raw = proc.stdout.read(frame_size)
                if len(raw) != frame_size:
                    break
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                    (self.FRAME_HEIGHT, self.FRAME_WIDTH, 3)
                )
                expected = bitc.encode_value(1_781_234_000.0 + offset / 100.0)
                decoded, crc_ok = bitc.decode_value(frame)
                if crc_ok and decoded == expected:
                    passed += 1
            rest = proc.stdout.read()
            stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            rc = proc.wait()
        if rc != 0:
            raise AssertionError(f"ffmpeg decode failed rc={rc}: {stderr}")
        if rest:
            raise AssertionError(f"ffmpeg decoded unexpected extra bytes: {len(rest)}")
        return passed


if __name__ == "__main__":
    unittest.main()
