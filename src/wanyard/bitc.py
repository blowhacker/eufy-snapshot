"""Burned-in timecode marker codec.

The marker is a bottom-left, bottom-flush strip of 46 black/white cells. The
payload is Unix time in centiseconds, followed by an 8-bit CRC.
"""

from __future__ import annotations

import zlib
from typing import overload

import numpy as np

CELL = 8
NPAY = 38
NCRC = 8
NCELLS = NPAY + NCRC
WIDTH = CELL * NCELLS
HEIGHT = CELL

_MAX_VALUE = 1 << NPAY
_WHITE = 255
_THRESHOLD = 128
_CHROMA_NEUTRAL = 128


def encode_value(unix_seconds: float) -> int:
    """Return Unix centiseconds for ``unix_seconds``.

    The value is encoded directly into the 38-bit payload.
    """
    value = int(round(float(unix_seconds) * 100.0))
    if value < 0:
        raise ValueError("BITC value must be non-negative")
    if value >= _MAX_VALUE:
        raise ValueError(f"BITC value exceeds {NPAY}-bit payload")
    return value


@overload
def geometry(frame_or_height: np.ndarray) -> tuple[int, int, int, int]:
    ...


@overload
def geometry(frame_or_height: int) -> tuple[int, int, int, int]:
    ...


def geometry(frame_or_height: np.ndarray | int) -> tuple[int, int, int, int]:
    """Return the marker rectangle as ``(x0, y0, width, height)``."""
    height = (
        int(frame_or_height.shape[0])
        if hasattr(frame_or_height, "shape")
        else int(frame_or_height)
    )
    return 0, height - CELL, WIDTH, HEIGHT


def render(frame_bgr: np.ndarray, value: int) -> np.ndarray:
    """Burn encoded centisecond ``value`` into ``frame_bgr``.

    The input frame is modified in place and returned.
    """
    frame = _validate_frame(frame_bgr)
    value = _validate_value(value)
    x0, y0, _, _ = geometry(frame)
    bits = _bits_for_value(value)
    for i, bit in enumerate(bits):
        x1 = x0 + i * CELL
        frame[y0 : y0 + CELL, x1 : x1 + CELL, ...] = _WHITE if bit else 0
    return frame_bgr


def render_time(frame_bgr: np.ndarray, unix_seconds: float) -> np.ndarray:
    """Burn ``unix_seconds`` into ``frame_bgr``."""
    return render(frame_bgr, encode_value(unix_seconds))


def render_yuv420(y: np.ndarray, u: np.ndarray, v: np.ndarray, value: int) -> None:
    """Burn ``value`` into yuv420p planes in place.

    The marker is black/white, i.e. pure luma: cells are written into the
    full-res ``y`` plane and the chroma planes are neutralised (128) over the
    strip so the marker reads back cleanly regardless of scene chroma. Each
    plane is a 2D ``uint8`` view ``(rows, stride)`` — column padding in the
    stride is left untouched.

    This avoids the bgr24 round-trip (two full-frame colourspace conversions
    per frame) that :func:`render` would force on a decoded yuv420p frame.
    """
    value = _validate_value(value)
    x0, y0, width, _ = geometry(int(y.shape[0]))
    bits = _bits_for_value(value)
    for i, bit in enumerate(bits):
        x1 = x0 + i * CELL
        y[y0 : y0 + CELL, x1 : x1 + CELL] = _WHITE if bit else 0
    cy0, cy1 = y0 // 2, (y0 + CELL) // 2
    cx0, cx1 = x0 // 2, (x0 + width) // 2
    u[cy0:cy1, cx0:cx1] = _CHROMA_NEUTRAL
    v[cy0:cy1, cx0:cx1] = _CHROMA_NEUTRAL


def decode(frame_bgr: np.ndarray) -> tuple[float | None, bool]:
    """Read a marker from ``frame_bgr``.

    Returns ``(unix_seconds, True)`` on a valid CRC, otherwise ``(None, False)``.
    """
    value, crc_ok = decode_value(frame_bgr)
    if not crc_ok or value is None:
        return None, False
    return value / 100.0, True


def decode_value(frame_bgr: np.ndarray) -> tuple[int | None, bool]:
    """Read the exact encoded centisecond value from ``frame_bgr``."""
    frame = np.asarray(frame_bgr)
    if not _can_read(frame):
        return None, False

    x0, y0, _, _ = geometry(frame)
    pad = CELL // 4
    bits: list[int] = []
    for i in range(NCELLS):
        x1 = x0 + i * CELL
        cell = frame[
            y0 + pad : y0 + CELL - pad,
            x1 + pad : x1 + CELL - pad,
            ...,
        ]
        bits.append(1 if float(cell.mean()) > _THRESHOLD else 0)

    value = _value_from_bits(bits[:NPAY])
    crc = _value_from_bits(bits[NPAY:])
    if crc != _crc8(value):
        return None, False
    return value, True


def mask(frame_bgr: np.ndarray) -> np.ndarray:
    """Zero exactly the marker strip rectangle in ``frame_bgr``."""
    frame = _validate_frame(frame_bgr)
    x0, y0, width, height = geometry(frame)
    frame[y0 : y0 + height, x0 : x0 + width, ...] = 0
    return frame_bgr


def _validate_frame(frame_bgr: np.ndarray) -> np.ndarray:
    frame = np.asarray(frame_bgr)
    if frame.dtype != np.uint8:
        raise TypeError("BITC frames must be uint8")
    if frame.ndim not in (2, 3):
        raise ValueError("BITC frames must be HxW or HxWxC")
    if frame.shape[0] < CELL or frame.shape[1] < WIDTH:
        raise ValueError(
            f"BITC frame too small for {WIDTH}x{HEIGHT} marker: "
            f"{frame.shape[1]}x{frame.shape[0]}"
        )
    return frame


def _can_read(frame: np.ndarray) -> bool:
    return (
        frame.dtype == np.uint8
        and frame.ndim in (2, 3)
        and frame.shape[0] >= CELL
        and frame.shape[1] >= WIDTH
    )


def _validate_value(value: int) -> int:
    if not isinstance(value, int):
        raise TypeError("BITC value must be an integer centisecond timestamp")
    if value < 0:
        raise ValueError("BITC value must be non-negative")
    if value >= _MAX_VALUE:
        raise ValueError(f"BITC value exceeds {NPAY}-bit payload")
    return value


def _bits_for_value(value: int) -> list[int]:
    crc = _crc8(value)
    return [(value >> i) & 1 for i in range(NPAY)] + [
        (crc >> i) & 1 for i in range(NCRC)
    ]


def _value_from_bits(bits: list[int]) -> int:
    value = 0
    for i, bit in enumerate(bits):
        value |= int(bit) << i
    return value


def _crc8(value: int) -> int:
    return zlib.crc32(value.to_bytes(8, "little", signed=False)) & 0xFF
