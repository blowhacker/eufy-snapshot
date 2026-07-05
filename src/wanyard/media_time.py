"""Media-time resolver — the single boundary between BITC time and media time.

See docs/media-time-architecture.md.

BITC time = (source_id, t), t = the decoded BITC/Unix seconds burned into the
frame. This is the only point-in-time coordinate that crosses module
boundaries. Media time = (asset, offset), exists only inside this module and
the player/decoder.

Hard rule: BITC time is the only public point-in-time coordinate. Recorded
media is anchored by segments.media_epoch, and media offsets stay private to
this resolver/player boundary.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

EPS = 0.05  # round-trip identity tolerance, seconds

# Live HLS fragment boundaries can land slightly away from decoded BITC frames.
_LIVE_EDGE_SLOP_SECONDS = 2.0
_LIVE_PROBE_CACHE_MAX = 128

# Mirrors VideoWorker._MAX_SEGMENT_SECONDS — used only to estimate when an open
# segment will close (i.e. when its authoritative MP4 frame becomes readable).
_MAX_SEGMENT_SECONDS_HINT = 600.0
# Grace after estimated close before the faststart MP4 is flushed + readable.
_CLOSE_READY_GRACE_SECONDS = 20.0

# The MP4 holds a hair more content than the wall recording span (end_ts -
# start_ts) because ffmpeg flushes a final GOP at close. Allow a small tail
# grace so a detection on that last fragment still resolves to its own file
# rather than a gap. Far below real reconnect gaps.
_COVERAGE_TAIL_GRACE_SECONDS = 0.5


@dataclass(frozen=True)
class _LiveBitcProbe:
    bitc_ts: float
    media_offset: float


_LIVE_PROBE_CACHE: dict[tuple[str, int, int], _LiveBitcProbe | None] = {}
_LIVE_PROBE_LOCK = threading.Lock()


def _recorded_media_epoch(row: sqlite3.Row) -> float | None:
    value = row["media_epoch"]
    if value is None:
        return None
    return float(value)


def _recorded_duration(row: sqlite3.Row) -> float | None:
    duration = row["duration_sec"]
    if duration is not None:
        return max(0.0, float(duration))
    if row["end_ts"] is None:
        return None
    return max(0.0, float(row["end_ts"]) - float(row["start_ts"]))


def _recorded_media_epoch_sql(alias: str | None = None) -> str:
    p = f"{alias}." if alias else ""
    return f"{p}media_epoch"


@dataclass(frozen=True)
class Anchor:
    """BITC<->media conversion for one playable asset.

    media_epoch is the BITC/Unix time of media_offset == 0.
    """
    provider: str            # "mp4" | "hls"
    asset_ref: str           # mp4 rel path, or live .ts filename
    media_epoch: float
    duration: float | None   # seconds; None if open/unknown

    def bitc_to_media(self, t: float) -> float:
        o = t - self.media_epoch
        if o < 0:
            return 0.0
        if self.duration is not None and o > self.duration:
            return self.duration
        return o

    def media_to_bitc(self, o: float) -> float:
        return self.media_epoch + o


@dataclass(frozen=True)
class Coverage:
    start: float
    end: float


@dataclass(frozen=True)
class MediaLocation:
    provider: str                  # "mp4" | "hls" | "none"
    url: str | None
    media_offset: float | None
    coverage: Coverage | None
    anchor: Anchor | None
    reason: str                    # "recorded" | "live" | "gap" | "no_anchor"
    segment_id: int | None = None  # DB segment id when provider is recorded MP4


def _none(reason: str, coverage: Coverage | None = None) -> MediaLocation:
    return MediaLocation(
        provider="none", url=None, media_offset=None,
        coverage=coverage, anchor=None, reason=reason,
    )


# ── recorded MP4 ────────────────────────────────────────────────────────────

def _resolve_recorded(conn: sqlite3.Connection, source_id: str,
                      t: float) -> MediaLocation | None:
    """Closed segment covering t. None = not recorded (caller tries live); a
    'none' MediaLocation = recorded region but unusable (no anchor).

    Single basis anchored at media_epoch. Duration is private
    media metadata. Coverage = [media_epoch, media_epoch + duration].
    """
    epoch = _recorded_media_epoch_sql()
    duration = "COALESCE(duration_sec, end_ts - start_ts)"
    row = conn.execute(
        "SELECT id, path, start_ts, end_ts,"
        f" duration_sec, {epoch} AS media_epoch"
        " FROM segments"
        " WHERE source_id=? AND end_ts IS NOT NULL"
        f"   AND {epoch} IS NOT NULL"
        f"   AND {epoch}<=?"
        f"   AND {epoch} + {duration} + ? >=?"
        f" ORDER BY {epoch} DESC LIMIT 1",
        (source_id, t, _COVERAGE_TAIL_GRACE_SECONDS, t),
    ).fetchone()
    if row:
        media_epoch = _recorded_media_epoch(row)
        if media_epoch is None:
            return _none("no_anchor")
        # Include the final-GOP flush tail so a detection on the last fragment
        # both selects (above) and clamps (bitc_to_media) to its own file.
        media_duration = (_recorded_duration(row) or 0.0) + _COVERAGE_TAIL_GRACE_SECONDS
        anchor = Anchor("mp4", row["path"], media_epoch, media_duration)
        return MediaLocation(
            provider="mp4",
            url=f"/video/files/{row['path']}",
            media_offset=anchor.bitc_to_media(t),
            coverage=Coverage(media_epoch, media_epoch + media_duration),
            anchor=anchor,
            reason="recorded",
            segment_id=int(row["id"]),
        )

    # No anchored coverage. If a closed segment nominally brackets t but has no
    # media_epoch, surface the unknown instead of guessing with start_ts.
    if conn.execute(
        f"SELECT 1 FROM segments"
        " WHERE source_id=? AND end_ts IS NOT NULL"
        f"   AND {epoch} IS NULL"
        "   AND start_ts<=? AND end_ts>=? LIMIT 1",
        (source_id, t, t),
    ).fetchone():
        return _none("no_anchor")
    return None


# ── live HLS ────────────────────────────────────────────────────────────────

def _safe_live_source_id(source_id: str | None) -> bool:
    return bool(
        source_id
        and ".." not in source_id
        and "/" not in source_id
        and "\\" not in source_id
    )


def _live_playlist_entries(video_dir: Path, source_id: str) -> tuple[object | None, list[dict]]:
    """Return playlist stat and media-offset entries.

    EXT-X-PROGRAM-DATE-TIME is intentionally ignored. It is a transport hint,
    not scene time. The only absolute anchor comes from decoded BITC markers.
    """
    if not _safe_live_source_id(source_id):
        return None, []
    playlist = video_dir / "live" / source_id / "live.m3u8"
    try:
        stat = playlist.stat()
        lines = playlist.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, []

    target = 0.0
    next_dur: float | None = None
    offset = 0.0
    entries: list[dict] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target = float(line.split(":", 1)[1])
            except (IndexError, ValueError):
                target = 0.0
        elif line.startswith("#EXTINF:"):
            try:
                next_dur = float(line.split(":", 1)[1].split(",", 1)[0])
            except (IndexError, ValueError):
                next_dur = None
        elif not line.startswith("#"):
            dur = next_dur if next_dur is not None else target
            if dur > 0:
                entries.append({
                    "uri": Path(line.split("?", 1)[0]).name,
                    "offset": offset,
                    "duration": dur,
                })
                offset += dur
            next_dur = None
    return stat, entries


def _frame_media_seconds(frame) -> float | None:
    frame_time = getattr(frame, "time", None)
    if frame_time is not None:
        try:
            return float(frame_time)
        except (TypeError, ValueError):
            pass
    pts = getattr(frame, "pts", None)
    time_base = getattr(frame, "time_base", None)
    if pts is None or time_base is None:
        return None
    try:
        return float(pts * time_base)
    except (TypeError, ValueError):
        return None


def _stream_rate(stream) -> float | None:
    for attr in ("average_rate", "base_rate", "guessed_rate"):
        rate = getattr(stream, attr, None)
        if not rate:
            continue
        try:
            value = float(rate)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if value > 0:
            return value
    return None


def _decode_live_bitc_probe(path: Path) -> _LiveBitcProbe | None:
    """Decode one BITC marker and its media offset within an HLS fragment."""
    from . import bitc, sei
    import av

    container = None
    try:
        container = av.open(str(path))
        video_stream = next((s for s in container.streams if s.type == "video"), None)
        if video_stream is None:
            return None
        rate = _stream_rate(video_stream)
        first_media_ts: float | None = None
        frame_index = 0
        for packet in container.demux(video_stream):
            for frame in packet.decode():
                frame_ts = _frame_media_seconds(frame)
                if first_media_ts is None and frame_ts is not None:
                    first_media_ts = frame_ts
                if frame_ts is not None and first_media_ts is not None:
                    rel = max(0.0, frame_ts - first_media_ts)
                elif rate is not None:
                    rel = frame_index / rate
                else:
                    rel = 0.0
                frame_index += 1

                # SEI-copy streams carry the clock in frame side data and do
                # not have a rendered pixel marker. Re-encoded streams and
                # existing archives still use the pixel marker fallback.
                marker, crc_ok = sei.decode_frame(frame)
                if not crc_ok:
                    frame_bgr = frame.to_ndarray(format="bgr24")
                    marker, crc_ok = bitc.decode(frame_bgr)
                if crc_ok and marker is not None:
                    return _LiveBitcProbe(float(marker), float(rel))
    except Exception:
        return None
    finally:
        if container is not None:
            try:
                container.close()
            except Exception:
                pass
    return None


def _cached_live_bitc_probe(path: Path) -> _LiveBitcProbe | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    with _LIVE_PROBE_LOCK:
        if key in _LIVE_PROBE_CACHE:
            return _LIVE_PROBE_CACHE[key]

    probe = _decode_live_bitc_probe(path)
    with _LIVE_PROBE_LOCK:
        if len(_LIVE_PROBE_CACHE) >= _LIVE_PROBE_CACHE_MAX:
            _LIVE_PROBE_CACHE.clear()
        _LIVE_PROBE_CACHE[key] = probe
    return probe


def _live_window_epoch(video_dir: Path, source_id: str, entries: list[dict]) -> float | None:
    live_dir = video_dir / "live" / source_id
    for entry in reversed(entries):
        probe = _cached_live_bitc_probe(live_dir / entry["uri"])
        if probe is not None:
            return probe.bitc_ts - (float(entry["offset"]) + probe.media_offset)
    return None


def _live_chunks_and_stat(video_dir: Path, source_id: str) -> tuple[list[dict], object | None]:
    stat, entries = _live_playlist_entries(video_dir, source_id)
    if not entries:
        return [], stat
    media_epoch = _live_window_epoch(video_dir, source_id, entries)
    if media_epoch is None:
        return [], stat

    chunks: list[dict] = []
    for entry in entries:
        start = media_epoch + float(entry["offset"])
        duration = float(entry["duration"])
        chunks.append({
            "uri": entry["uri"],
            "start_ts": start,
            "end_ts": start + duration,
            "duration": duration,
            "media_offset": float(entry["offset"]),
        })
    return chunks, stat


def _live_chunks(video_dir: Path, source_id: str) -> list[dict]:
    """[{uri, start_ts(BITC), end_ts, duration}] for the live playlist."""
    chunks, _stat = _live_chunks_and_stat(video_dir, source_id)
    return chunks


def live_window(video_dir: Path, source_id: str | None) -> dict | None:
    """Public live HLS DVR window, anchored on decoded BITC time."""
    if not source_id:
        return None
    chunks, stat = _live_chunks_and_stat(video_dir, source_id)
    if not chunks:
        return None
    start_ts = chunks[0]["start_ts"]
    end_ts = max(c["end_ts"] for c in chunks)
    return {
        "source_id": source_id,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "duration": max(0.0, end_ts - start_ts),
        "segment_count": len(chunks),
        "playlist_age_seconds": (
            max(0.0, time.time() - stat.st_mtime) if stat is not None else None
        ),
        "segments": chunks,
    }


def _resolve_live(video_dir: Path, source_id: str,
                  t: float) -> MediaLocation | None:
    chunks = _live_chunks(video_dir, source_id)
    if not chunks:
        return None
    window = Coverage(chunks[0]["start_ts"],
                      max(c["end_ts"] for c in chunks))
    for c in chunks:
        if (t >= c["start_ts"] - _LIVE_EDGE_SLOP_SECONDS
                and t <= c["end_ts"] + _LIVE_EDGE_SLOP_SECONDS):
            anchor = Anchor("hls", c["uri"], c["start_ts"], c["duration"])
            return MediaLocation(
                provider="hls",
                url=f"/video/live/{source_id}/{c['uri']}",
                media_offset=anchor.bitc_to_media(t),
                coverage=window,
                anchor=anchor,
                reason="live",
            )
    return None  # within neither a chunk; caller decides gap vs nothing


# ── gap nearest coverage ────────────────────────────────────────────────────

def _nearest_coverage(conn: sqlite3.Connection, source_id: str,
                      t: float) -> Coverage | None:
    epoch = _recorded_media_epoch_sql()
    before = conn.execute(
        "SELECT start_ts, end_ts, duration_sec,"
        f" {epoch} AS media_epoch"
        " FROM segments"
        f" WHERE source_id=? AND {epoch} IS NOT NULL AND {epoch}<=?"
        f" ORDER BY {epoch} DESC LIMIT 1",
        (source_id, t),
    ).fetchone()
    after = conn.execute(
        "SELECT start_ts, end_ts, duration_sec,"
        f" {epoch} AS media_epoch"
        " FROM segments"
        f" WHERE source_id=? AND {epoch} IS NOT NULL AND {epoch}>?"
        f" ORDER BY {epoch} ASC LIMIT 1",
        (source_id, t),
    ).fetchone()

    def cov(row) -> Coverage | None:
        if not row:
            return None
        epoch = _recorded_media_epoch(row)
        if epoch is None:
            return None
        end = row["end_ts"]
        if end is None:
            return Coverage(float(epoch), float(epoch))
        return Coverage(float(epoch), float(epoch) + (_recorded_duration(row) or 0.0))

    cands = [c for c in (cov(before), cov(after)) if c]
    if not cands:
        return None
    return min(cands, key=lambda c: min(abs(c.start - t), abs(c.end - t)))


# ── public entry point ──────────────────────────────────────────────────────

def resolve(conn: sqlite3.Connection, video_dir: Path,
            source_id: str, t: float) -> MediaLocation:
    """The only place BITC time is converted to media time.

    Closed recorded MP4 wins for the past; live HLS for the recent edge
    (including the currently-open, not-yet-finalized segment); otherwise a gap.
    """
    recorded = _resolve_recorded(conn, source_id, t)
    if recorded is not None and recorded.provider == "mp4":
        return recorded

    live = _resolve_live(video_dir, source_id, t)
    if live is not None:
        return live

    # Recorded-but-unusable (no_anchor) takes precedence over a bare gap so the
    # unknown is not silently masked.
    if recorded is not None:
        return recorded

    return _none("gap", _nearest_coverage(conn, source_id, t))


# ── extraction face: the single frame reader ────────────────────────────────

@dataclass(frozen=True)
class FrameResult:
    frame: object | None       # numpy ndarray (BGR) or None
    status: str                # "ok" | "pending" | "gap" | "no_anchor"
    provider: str              # "mp4" | "hls" | "none"
    retry_after: float | None  # for "pending": unix ts when the authoritative frame exists


def _decode_mp4_frame(path: Path, offset: float):
    """Indexed MP4 → seek by time and decode. Returns ndarray or None."""
    import cv2
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, offset) * 1000.0)
        ok, frame = cap.read()
        return frame if ok and frame is not None else None
    finally:
        cap.release()


def _decode_ts_frame_at_bitc(path: Path, t: float, max_drift: float = 0.5):
    """Return the HLS frame whose decoded BITC marker is nearest ``t``."""
    import cv2

    from . import bitc

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None
    try:
        best_frame = None
        best_diff = max_drift
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            marker, crc_ok = bitc.decode(frame)
            if not crc_ok or marker is None:
                continue
            diff = abs(float(marker) - t)
            if diff < best_diff:
                best_diff = diff
                best_frame = frame
                if diff < 0.02:
                    break
        return best_frame
    finally:
        cap.release()


def _expected_close_ts(conn: sqlite3.Connection, source_id: str,
                       t: float) -> float | None:
    """When the open segment covering t will close and its MP4 be readable."""
    row = conn.execute(
        "SELECT start_ts FROM segments"
        " WHERE source_id=? AND end_ts IS NULL AND start_ts<=?"
        " ORDER BY start_ts DESC LIMIT 1",
        (source_id, t),
    ).fetchone()
    if not row:
        return None
    return float(row["start_ts"]) + _MAX_SEGMENT_SECONDS_HINT + _CLOSE_READY_GRACE_SECONDS


def read_frame(conn: sqlite3.Connection, video_dir: Path,
               source_id: str, t: float) -> FrameResult:
    """The single frame reader behind the (source_id, t) boundary.

    No consumer (confirmation, backfill, thumbnails) decodes media itself; the
    provider choice, frame-accurate seek, and availability policy all live here.
    The authoritative frame for a past instant is the closed MP4; while a segment
    is still open the live .ts is best-effort, and if it is not fetchable the
    result is `pending(retry_after=close)` rather than a wrong or blank frame.
    """
    loc = resolve(conn, video_dir, source_id, t)

    if loc.provider == "mp4":
        path = video_dir / loc.anchor.asset_ref
        frame = _decode_mp4_frame(path, loc.media_offset)
        if frame is not None:
            return FrameResult(frame, "ok", "mp4", None)
        # Closed file unreadable (rare) — let caller retry shortly.
        return FrameResult(None, "pending", "mp4", t + _CLOSE_READY_GRACE_SECONDS)

    if loc.provider == "hls":
        path = video_dir / "live" / source_id / loc.anchor.asset_ref
        frame = _decode_ts_frame_at_bitc(path, t)
        if frame is not None:
            return FrameResult(frame, "ok", "hls", None)
        # Live chunk not fetchable; authoritative MP4 exists after segment close.
        return FrameResult(None, "pending", "hls", _expected_close_ts(conn, source_id, t))

    # provider "none": gap or no_anchor — propagate honestly.
    return FrameResult(None, loc.reason, "none", None)


# ── BITC round-trip invariant check ─────────────────────────────────────────

@dataclass(frozen=True)
class RoundTrip:
    ok: bool
    detection_id: int
    status: str               # ok | sentinel | no_anchor | gap | world_mismatch | no_detection
    world_delta: float | None  # |resolved BITC - detection BITC|, the real invariant
    expected_offset: float
    resolved_offset: float | None
    provider: str
    alternate: bool           # resolved a valid but different segment (boundary duplicate)


def check_detection_round_trip(conn: sqlite3.Connection, video_dir: Path,
                               detection_id: int) -> RoundTrip:
    """Invariant: resolve(media->BITC(detection)) lands on a usable asset whose
    BITC time equals the detection's BITC time, within EPS.

    The invariant is on BITC time, not offset equality: at contiguous segment
    boundaries the same BITC instant exists in two files, so resolving to the
    adjacent segment (different offset) is correct. The expected media offset is
    derived (abs_ts - media_epoch), never stored. No side effects.
    """
    row = conn.execute(
        "SELECT vd.id, vd.source_id, vd.abs_ts,"
        " (vd.abs_ts - s.media_epoch) AS media_offset"
        " FROM video_detections vd JOIN segments s ON s.id=vd.segment_id"
        " WHERE vd.id=?",
        (detection_id,),
    ).fetchone()
    if not row or row["media_offset"] is None:
        return RoundTrip(False, detection_id, "no_detection", None,
                         0.0, None, "none", False)

    expected = float(row["media_offset"])
    bitc_t = float(row["abs_ts"])
    loc = resolve(conn, video_dir, row["source_id"], bitc_t)
    if loc.provider != "mp4" or loc.anchor is None or loc.media_offset is None:
        return RoundTrip(False, detection_id, loc.reason, None,
                         expected, loc.media_offset, loc.provider, False)
    resolved_bitc = loc.anchor.media_to_bitc(loc.media_offset)
    bitc_delta = abs(resolved_bitc - bitc_t)
    ok = bitc_delta < EPS
    alternate = abs(loc.media_offset - expected) > EPS
    return RoundTrip(ok, detection_id, "ok" if ok else "world_mismatch",
                     bitc_delta, expected, loc.media_offset, loc.provider, alternate)
