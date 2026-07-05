from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard import sei
from wanyard.bitc import encode_value
from wanyard.db import SourceDB, RtspSourceRow


class PayloadTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        value = encode_value(1_760_000_000.25)
        self.assertEqual(sei.parse_payload(sei.build_payload(value)), value)

    def test_wrong_uuid_reads_absent(self) -> None:
        value = encode_value(1_760_000_000.0)
        payload = bytearray(sei.build_payload(value))
        payload[0] ^= 0xFF
        self.assertIsNone(sei.parse_payload(bytes(payload)))

    def test_bad_crc_reads_absent_never_wrong(self) -> None:
        value = encode_value(1_760_000_000.0)
        payload = bytearray(sei.build_payload(value))
        payload[20] ^= 0x01   # corrupt a value byte; crc no longer matches
        self.assertIsNone(sei.parse_payload(bytes(payload)))

    def test_truncated_reads_absent(self) -> None:
        value = encode_value(1_760_000_000.0)
        self.assertIsNone(sei.parse_payload(sei.build_payload(value)[:-1]))

    def test_out_of_range_value_rejected(self) -> None:
        with self.assertRaises(ValueError):
            sei.build_payload(1 << 40)
        with self.assertRaises(ValueError):
            sei.build_payload(-1)


class NalTests(unittest.TestCase):
    def test_no_start_code_emulation_in_nal(self) -> None:
        # A value whose LE bytes contain 00 00 00 runs must be escaped so the
        # NAL body never contains a start-code-like sequence.
        for value in (0, 1, 0x0100, 0x010000, encode_value(1_760_000_000.0)):
            nal = sei.sei_nal(value)
            self.assertNotIn(b"\x00\x00\x01", nal)
            self.assertNotIn(b"\x00\x00\x00", nal)

    def test_nal_header_is_sei(self) -> None:
        nal = sei.sei_nal(0)
        self.assertEqual(nal[0] & 0x1F, 6)


def _annexb_au(*, with_prefix_nals: bool) -> bytes:
    """Synthetic H.264 access unit: optional SPS/PPS, then an IDR slice."""
    sps = b"\x00\x00\x00\x01\x67\x42\x00\x1e\xab\x40"
    pps = b"\x00\x00\x00\x01\x68\xce\x38\x80"
    idr = b"\x00\x00\x00\x01\x65\x88\x84\x00\x33\xff"
    return (sps + pps + idr) if with_prefix_nals else idr


class InjectTests(unittest.TestCase):
    def test_annexb_sei_lands_before_slice_after_paramsets(self) -> None:
        value = encode_value(1_760_000_000.0)
        au = _annexb_au(with_prefix_nals=True)
        out = sei.inject(au, value)
        self.assertIsNotNone(out)
        # NAL order: SPS, PPS, SEI, IDR
        types = []
        i = 0
        while i < len(out) - 4:
            if out[i:i + 4] == b"\x00\x00\x00\x01":
                types.append(out[i + 4] & 0x1F)
                i += 4
            else:
                i += 1
        self.assertEqual(types, [7, 8, 6, 5])

    def test_annexb_slice_only(self) -> None:
        value = encode_value(1_760_000_000.0)
        out = sei.inject(_annexb_au(with_prefix_nals=False), value)
        self.assertIsNotNone(out)
        self.assertEqual(out[4] & 0x1F, 6)

    def test_avcc(self) -> None:
        value = encode_value(1_760_000_000.0)
        slice_nal = b"\x65\x88\x84\x00\x33\xff"
        au = len(slice_nal).to_bytes(4, "big") + slice_nal
        out = sei.inject(au, value)
        self.assertIsNotNone(out)
        first_len = int.from_bytes(out[:4], "big")
        self.assertEqual(out[4] & 0x1F, 6)
        # original slice follows intact
        rest = out[4 + first_len:]
        self.assertEqual(rest, au)

    def test_no_slice_returns_none(self) -> None:
        value = encode_value(1_760_000_000.0)
        sps_only = b"\x00\x00\x00\x01\x67\x42\x00\x1e"
        self.assertIsNone(sei.inject(sps_only, value))

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(sei.inject(b"\x12\x34\x56\x78" * 4, 0))


class StampModeTests(unittest.TestCase):
    def _db(self, tmpdir: str) -> SourceDB:
        db = SourceDB(Path(tmpdir) / "sources.db")
        db.insert(RtspSourceRow(
            id="front", name="front", url="rtsp://cam/front",
            interval_seconds=60, enabled=True,
            rtsp_transport="tcp", timeout_seconds=10, output_subdir="front",
        ))
        return db

    def test_default_is_reencode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(sei.stamp_mode(self._db(tmpdir), "front"),
                             "reencode")

    def test_source_override_beats_global(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._db(tmpdir)
            db.set_setting(sei.STAMP_MODE_GLOBAL_KEY, "sei_copy")
            self.assertEqual(sei.stamp_mode(db, "front"), "sei_copy")
            db.set_setting(sei.stamp_mode_key("front"), "reencode")
            self.assertEqual(sei.stamp_mode(db, "front"), "reencode")

    def test_garbage_setting_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._db(tmpdir)
            db.set_setting(sei.stamp_mode_key("front"), "banana")
            self.assertEqual(sei.stamp_mode(db, "front"), "reencode")

    def test_normalize_aliases(self) -> None:
        self.assertEqual(sei.normalize_stamp_mode("SEI"), "sei_copy")
        self.assertEqual(sei.normalize_stamp_mode("copy"), "sei_copy")
        self.assertEqual(sei.normalize_stamp_mode("pixel"), "reencode")
        self.assertIsNone(sei.normalize_stamp_mode("banana"))


class TimelineNudgeTests(unittest.TestCase):
    def test_nudge_emits_monotonic_pts_for_stale_samples(self) -> None:
        from wanyard.stamper import _BitcMediaTimeline

        tl = _BitcMediaTimeline(max_gap_seconds=2.0)
        self.assertEqual(tl.pts(1_000.0, nudge=True), 0)
        self.assertEqual(tl.pts(1_000.1, nudge=True), 9_000)
        # stale / repeated clock: +1 tick, never None, never backwards
        self.assertEqual(tl.pts(1_000.1, nudge=True), 9_001)
        self.assertEqual(tl.pts(1_000.05, nudge=True), 9_002)
        # clock resumes: real pts again
        self.assertEqual(tl.pts(1_000.2, nudge=True), 18_000)

    def test_nudge_still_raises_on_real_gap(self) -> None:
        from wanyard.stamper import _BitcEpochDiscontinuity, _BitcMediaTimeline

        tl = _BitcMediaTimeline(max_gap_seconds=2.0)
        tl.pts(1_000.0, nudge=True)
        with self.assertRaises(_BitcEpochDiscontinuity):
            tl.pts(1_003.0, nudge=True)

    def test_default_mode_unchanged(self) -> None:
        from wanyard.stamper import _BitcMediaTimeline

        tl = _BitcMediaTimeline(max_gap_seconds=2.0)
        self.assertEqual(tl.pts(1_000.0), 0)
        self.assertIsNone(tl.pts(1_000.0))
        self.assertIsNone(tl.pts(999.9))
        self.assertEqual(tl.pts(1_000.1), 9_000)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available")
class EndToEndTests(unittest.TestCase):
    """Prod-shaped chain: h264 annexb packets -> sei.inject per AU ->
    mpegts mux (annexb transport, like RTSP) -> ffmpeg -c copy to MP4
    (the recorder) -> decoded-frame side data (detector + anchor)."""

    EPOCH = 1_760_000_000.0
    FPS = 10

    def _build_mp4(self, tmpdir: Path) -> tuple[Path, list[int]]:
        import av

        raw264 = tmpdir / "in.264"
        out = av.open(str(raw264), "w", format="h264")
        vs = out.add_stream("libx264", rate=self.FPS)
        vs.width, vs.height, vs.pix_fmt = 320, 240, "yuv420p"
        vs.options = {"bf": "0", "g": "5"}   # cameras: no B-frames
        for _ in range(12):
            for p in vs.encode(av.VideoFrame(320, 240, "yuv420p")):
                out.mux(p)
        for p in vs.encode(None):
            out.mux(p)
        out.close()

        ts_path = tmpdir / "stamped.ts"
        src = av.open(str(raw264), format="h264",
                      options={"framerate": str(self.FPS)})
        dst = av.open(str(ts_path), "w", format="mpegts")
        ds = dst.add_stream_from_template(src.streams.video[0])
        values = []
        i = 0
        for pkt in src.demux():
            if not pkt.size:
                continue   # demuxer flush packet
            value = encode_value(self.EPOCH + i / self.FPS)
            injected = sei.inject(bytes(pkt), value)
            self.assertIsNotNone(injected, "injection must find the slice NAL")
            op = av.Packet(injected)
            op.pts = i * (90_000 // self.FPS)
            op.dts = op.pts
            op.time_base = Fraction(1, 90_000)
            op.stream = ds
            op.is_keyframe = pkt.is_keyframe
            dst.mux(op)
            values.append(value)
            i += 1
        dst.close()
        src.close()

        mp4_path = tmpdir / "seg.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(ts_path), "-c:v", "copy", "-movflags", "+faststart",
             str(mp4_path)],
            check=True,
        )
        return mp4_path, values

    def test_values_survive_copy_and_bind_to_frames(self) -> None:
        import av

        with tempfile.TemporaryDirectory() as tmpdir:
            mp4_path, values = self._build_mp4(Path(tmpdir))
            got = []
            with av.open(str(mp4_path)) as container:
                for frame in container.decode(video=0):
                    got.append(sei.decode_value_frame(frame))
            self.assertEqual(got, values)

    def test_recorder_anchor_reads_sei(self) -> None:
        import av

        from wanyard.video import _decode_bitc_media_epoch

        with tempfile.TemporaryDirectory() as tmpdir:
            mp4_path, _ = self._build_mp4(Path(tmpdir))
            epoch = _decode_bitc_media_epoch(mp4_path)
            self.assertIsNotNone(epoch)
            # media_epoch + first frame pts must equal the first frame's SEI
            # time — the anchor invariant, independent of muxer pts offsets.
            with av.open(str(mp4_path)) as container:
                frame = next(container.decode(video=0))
                first_pts = float(frame.pts * frame.time_base)
            self.assertAlmostEqual(epoch + first_pts, self.EPOCH, delta=0.02)

    def test_detector_style_decode_frame(self) -> None:
        import av

        with tempfile.TemporaryDirectory() as tmpdir:
            mp4_path, values = self._build_mp4(Path(tmpdir))
            with av.open(str(mp4_path)) as container:
                frame = next(container.decode(video=0))
                abs_ts, crc_ok = sei.decode_frame(frame)
            self.assertTrue(crc_ok)
            self.assertAlmostEqual(abs_ts, values[0] / 100.0, delta=1e-9)


if __name__ == "__main__":
    unittest.main()
