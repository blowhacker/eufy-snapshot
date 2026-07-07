"""Frame clock over SEI: the timecode value carried as an H.264 SEI NAL.

The clock is Unix centiseconds + CRC8, carried as an unregistered user-data
SEI inside each access unit (the pixel-burned carrier it replaced is gone).
Injection is packet-level — no decode, no encode — so
the stamped stream keeps the camera's original bits: zero generation loss,
archive size = camera native, and no NVENC session.

Binding is structural: the SEI NAL lives inside its access unit, so through
any remux/copy the value stays attached to exactly its own frame — it can be
stripped (by a transcode), never misattached. Trust-or-discard like the pixel
marker: wrong UUID or bad CRC reads as absent, never as a wrong time.

H.264 only. Cameras emit H.264 and sei_copy never re-encodes, so no HEVC
variant is needed; a non-H.264 input falls back to the re-encode stamper.
"""

from __future__ import annotations

import uuid as _uuid
import zlib

# ── Value codec (Unix centiseconds + CRC8) ───────────────────────────────────
# Formerly bitc._MAX_VALUE/_crc8/encode_value: the payload semantics, now that
# the pixel carrier is gone and the SEI NAL is the only carrier.
_NPAY = 38
_MAX_VALUE = 1 << _NPAY


def encode_value(unix_seconds: float) -> int:
    """Return Unix centiseconds for ``unix_seconds`` (the 38-bit payload)."""
    value = int(round(float(unix_seconds) * 100.0))
    if value < 0:
        raise ValueError("clock value must be non-negative")
    if value >= _MAX_VALUE:
        raise ValueError(f"clock value exceeds {_NPAY}-bit payload")
    return value


def _crc8(value: int) -> int:
    return zlib.crc32(value.to_bytes(8, "little", signed=False)) & 0xFF


# Application identity for the unregistered SEI (ITU-T H.264 D.1.7 / D.2.7).
# Consumers ignore any unregistered SEI whose UUID differs.
PAYLOAD_UUID = _uuid.UUID("6b616e79-6172-4264-9d74-c3b1c39c05e1").bytes

# uuid(16) + centiseconds u64 LE (8) + crc8(1)
_PAYLOAD_LEN = 25
_SEI_NAL_TYPE = 6
_SEI_TYPE_USER_DATA_UNREGISTERED = 5
_VCL_NAL_TYPES = frozenset({1, 2, 3, 4, 5})   # coded slice NALs
_ANNEXB_PREFIXES = (b"\x00\x00\x00\x01", b"\x00\x00\x01")

STAMP_MODE_REENCODE = "reencode"
STAMP_MODE_SEI_COPY = "sei_copy"
STAMP_MODES = (STAMP_MODE_REENCODE, STAMP_MODE_SEI_COPY)
STAMP_MODE_SOURCE_PREFIX = "stamp_mode_source:"
STAMP_MODE_GLOBAL_KEY = "stamp_mode_global"


# ── Stamp mode setting (source db; mirrors retention.record_mode) ────────────

def stamp_mode_key(source_id: str) -> str:
    return f"{STAMP_MODE_SOURCE_PREFIX}{source_id}"


def normalize_stamp_mode(value, default: str | None = None) -> str | None:
    if value is None:
        return default
    mode = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"sei": STAMP_MODE_SEI_COPY, "copy": STAMP_MODE_SEI_COPY,
               "pixel": STAMP_MODE_REENCODE, "bitc": STAMP_MODE_REENCODE}
    mode = aliases.get(mode, mode)
    return mode if mode in STAMP_MODES else default


def stamp_mode(settings, source_id: str, default: str = STAMP_MODE_SEI_COPY) -> str:
    """Effective stamp mode: per-source override > global setting > default.

    Default is sei_copy — codec-copy with the clock as SEI: zero generation
    loss, archive = camera bits, no GPU. The stamper still falls back to
    reencode automatically for non-h264 input, and reencode remains settable
    per source (DB/env) as the forensic visible-timecode mode.
    """
    override = normalize_stamp_mode(settings.get_setting(stamp_mode_key(source_id)))
    if override:
        return override
    return normalize_stamp_mode(
        settings.get_setting(STAMP_MODE_GLOBAL_KEY), default
    )


# ── Payload ──────────────────────────────────────────────────────────────────

def build_payload(value: int) -> bytes:
    """uuid + centisecond value + CRC8 — the unregistered SEI body."""
    if not isinstance(value, int) or value < 0 or value >= _MAX_VALUE:
        raise ValueError("SEI clock value out of range")
    return PAYLOAD_UUID + value.to_bytes(8, "little") + bytes([_crc8(value)])


def parse_payload(data: bytes) -> int | None:
    """Value from an unregistered-SEI body, or None (wrong app / bad CRC)."""
    if len(data) != _PAYLOAD_LEN or data[:16] != PAYLOAD_UUID:
        return None
    value = int.from_bytes(data[16:24], "little")
    if data[24] != _crc8(value):
        return None
    return value


# ── NAL construction ─────────────────────────────────────────────────────────

def _rbsp_escape(raw: bytes) -> bytes:
    """Insert emulation-prevention bytes (00 00 0x -> 00 00 03 0x)."""
    out = bytearray()
    zeros = 0
    for b in raw:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def _rbsp_unescape(raw: bytes) -> bytes:
    """Remove H.264 emulation-prevention bytes from an RBSP."""
    out = bytearray()
    zeros = 0
    i = 0
    while i < len(raw):
        b = raw[i]
        if zeros >= 2 and b == 3 and i + 1 < len(raw) and raw[i + 1] <= 3:
            zeros = 0
            i += 1
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
        i += 1
    return bytes(out)


def sei_nal(value: int) -> bytes:
    """Raw SEI NAL (no start code / length prefix): header + escaped RBSP."""
    body = build_payload(value)
    # payload type 5, payload size, body, rbsp trailing stop bit
    rbsp = bytes([_SEI_TYPE_USER_DATA_UNREGISTERED, len(body)]) + body + b"\x80"
    return bytes([_SEI_NAL_TYPE]) + _rbsp_escape(rbsp)


# ── Packet injection (Annex-B and AVCC) ──────────────────────────────────────

def _annexb_nal_offsets(buf: bytes) -> list[tuple[int, int]]:
    """(start-code offset, header offset) for each NAL in an Annex-B buffer."""
    out = []
    i = 0
    n = len(buf)
    while i < n - 2:
        if buf[i] == 0 and buf[i + 1] == 0:
            if buf[i + 2] == 1:
                out.append((i, i + 3))
                i += 3
                continue
            if i < n - 3 and buf[i + 2] == 0 and buf[i + 3] == 1:
                out.append((i, i + 4))
                i += 4
                continue
        i += 1
    return out


def inject(packet_bytes: bytes, value: int) -> bytes | None:
    """Insert our SEI NAL before the first coded-slice NAL of the access unit.

    One video packet = one access unit (ffmpeg demux). Handles both Annex-B
    (RTSP) and AVCC (4-byte length prefixes) framing. Returns None when no
    slice NAL is found or the framing is unparseable — the caller passes the
    packet through unmodified (missing time is loud downstream; a mangled
    bitstream is not).
    """
    buf = packet_bytes
    if buf.startswith(_ANNEXB_PREFIXES[0]) or buf.startswith(_ANNEXB_PREFIXES[1]):
        offsets = _annexb_nal_offsets(buf)
        for start, hdr in offsets:
            if (buf[hdr] & 0x1F) in _VCL_NAL_TYPES:
                nal = b"\x00\x00\x00\x01" + sei_nal(value)
                return buf[:start] + nal + buf[start:]
        return None

    # AVCC: length-prefixed NALs (4-byte, the practical universal case).
    i, n = 0, len(buf)
    while i + 4 <= n:
        length = int.from_bytes(buf[i:i + 4], "big")
        if length <= 0 or i + 4 + length > n:
            return None
        if (buf[i + 4] & 0x1F) in _VCL_NAL_TYPES:
            nal = sei_nal(value)
            return buf[:i] + len(nal).to_bytes(4, "big") + nal + buf[i:]
        i += 4 + length
    return None


def _value_from_sei_nal(nal: bytes) -> int | None:
    if not nal or (nal[0] & 0x1F) != _SEI_NAL_TYPE:
        return None
    rbsp = _rbsp_unescape(nal[1:])
    i = 0
    while i < len(rbsp):
        payload_type = 0
        while i < len(rbsp):
            b = rbsp[i]
            i += 1
            payload_type += b
            if b != 0xFF:
                break
        else:
            return None

        payload_size = 0
        while i < len(rbsp):
            b = rbsp[i]
            i += 1
            payload_size += b
            if b != 0xFF:
                break
        else:
            return None

        if payload_size > len(rbsp) - i:
            return None
        payload = rbsp[i:i + payload_size]
        i += payload_size
        if payload_type == _SEI_TYPE_USER_DATA_UNREGISTERED:
            value = parse_payload(payload)
            if value is not None:
                return value
    return None


def extract(packet_bytes: bytes) -> int | None:
    """Read our exact centisecond value from one H.264 access unit.

    Accepts the same Annex-B and four-byte AVCC framing as :func:`inject`.
    This is packet-level and does not decode video, so it is suitable for
    building a browser frame clock from a finalized MP4.
    """
    buf = packet_bytes
    if buf.startswith(_ANNEXB_PREFIXES[0]) or buf.startswith(_ANNEXB_PREFIXES[1]):
        offsets = _annexb_nal_offsets(buf)
        for index, (_start, header) in enumerate(offsets):
            end = offsets[index + 1][0] if index + 1 < len(offsets) else len(buf)
            value = _value_from_sei_nal(buf[header:end])
            if value is not None:
                return value
        return None

    i, n = 0, len(buf)
    while i + 4 <= n:
        length = int.from_bytes(buf[i:i + 4], "big")
        if length <= 0 or i + 4 + length > n:
            return None
        value = _value_from_sei_nal(buf[i + 4:i + 4 + length])
        if value is not None:
            return value
        i += 4 + length
    return None


# ── Decode side ──────────────────────────────────────────────────────────────

def decode_value_frame(frame) -> int | None:
    """Exact centisecond value from a decoded frame's SEI side data, or None."""
    try:
        side_data = frame.side_data
    except AttributeError:
        return None
    for sd in side_data:
        try:
            if sd.type.name != "SEI_UNREGISTERED":
                continue
            value = parse_payload(bytes(sd))
        except Exception:
            continue
        if value is not None:
            return value
    return None


def decode_frame(frame) -> tuple[float | None, bool]:
    """(unix_seconds, True) from frame side data, else (None, False)."""
    value = decode_value_frame(frame)
    if value is None:
        return None, False
    return value / 100.0, True


__all__ = [
    "PAYLOAD_UUID",
    "STAMP_MODE_REENCODE",
    "STAMP_MODE_SEI_COPY",
    "STAMP_MODES",
    "STAMP_MODE_GLOBAL_KEY",
    "build_payload",
    "parse_payload",
    "sei_nal",
    "inject",
    "extract",
    "decode_frame",
    "decode_value_frame",
    "encode_value",
    "normalize_stamp_mode",
    "stamp_mode",
    "stamp_mode_key",
]
