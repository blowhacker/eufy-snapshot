"""Media-time resolver — the single boundary between world time and media time.

See docs/media-time-architecture.md.

World time = (source_id, t), t = unix UTC seconds. The only coordinate that
crosses module boundaries. Media time = (asset, offset), exists only inside this
module and the player/decoder.

Hard rule: world time is the only public point-in-time coordinate. Recorded
media is anchored by segments.media_start_ts, and media offsets stay private to
this resolver/player boundary.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

EPS = 0.05  # round-trip identity tolerance, seconds

# Live HLS clock can lead/lag the wall clock slightly at chunk edges.
_LIVE_EDGE_SLOP_SECONDS = 2.0

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


def _recorded_media_epoch(row: sqlite3.Row) -> float | None:
    value = row["media_start_ts"]
    if value is None:
        value = row["actual_start_ts"]
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
    return f"COALESCE({p}media_start_ts, {p}actual_start_ts)"


@dataclass(frozen=True)
class Anchor:
    """world<->media conversion for one playable asset.

    media_epoch is the world time (unix UTC) of media_offset == 0.
    """
    provider: str            # "mp4" | "hls"
    asset_ref: str           # mp4 rel path, or live .ts filename
    media_epoch: float
    duration: float | None   # seconds; None if open/unknown

    def world_to_media(self, t: float) -> float:
        o = t - self.media_epoch
        if o < 0:
            return 0.0
        if self.duration is not None and o > self.duration:
            return self.duration
        return o

    def media_to_world(self, o: float) -> float:
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

    Single basis anchored at media_epoch = media_start_ts. Duration is private
    media metadata. Coverage = [media_start_ts, media_start_ts + duration].
    """
    epoch = _recorded_media_epoch_sql()
    duration = "COALESCE(duration_sec, end_ts - start_ts)"
    row = conn.execute(
        "SELECT id, path, start_ts, end_ts, media_start_ts, actual_start_ts,"
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
        # both selects (above) and clamps (world_to_media) to its own file.
        media_duration = (_recorded_duration(row) or 0.0) + _COVERAGE_TAIL_GRACE_SECONDS
        anchor = Anchor("mp4", row["path"], media_epoch, media_duration)
        return MediaLocation(
            provider="mp4",
            url=f"/video/files/{row['path']}",
            media_offset=anchor.world_to_media(t),
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

def _parse_pdt(raw: str) -> float | None:
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    if len(value) >= 5 and value[-5] in "+-" and value[-3] != ":":
        value = value[:-2] + ":" + value[-2:]
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _live_chunks(video_dir: Path, source_id: str) -> list[dict]:
    """[{uri, start_ts(PDT), end_ts, duration}] for the live playlist, or []."""
    playlist = video_dir / "live" / source_id / "live.m3u8"
    try:
        lines = playlist.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    target = 0.0
    next_start: float | None = None
    next_dur: float | None = None
    inferred: float | None = None
    chunks: list[dict] = []
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
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            next_start = _parse_pdt(line.split(":", 1)[1])
        elif not line.startswith("#"):
            dur = next_dur if next_dur is not None else target
            start = next_start if next_start is not None else inferred
            if start is not None and dur > 0:
                chunks.append({
                    "uri": Path(line.split("?", 1)[0]).name,
                    "start_ts": start,
                    "end_ts": start + dur,
                    "duration": dur,
                })
                inferred = start + dur
            next_start = None
            next_dur = None
    return chunks


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
            # PDT IS the media_epoch — HLS carries the honest anchor natively.
            anchor = Anchor("hls", c["uri"], c["start_ts"], c["duration"])
            return MediaLocation(
                provider="hls",
                url=f"/video/live/{source_id}/{c['uri']}",
                media_offset=anchor.world_to_media(t),
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
        "SELECT media_start_ts, actual_start_ts, start_ts, end_ts, duration_sec,"
        f" {epoch} AS media_epoch"
        " FROM segments"
        f" WHERE source_id=? AND {epoch} IS NOT NULL AND {epoch}<=?"
        f" ORDER BY {epoch} DESC LIMIT 1",
        (source_id, t),
    ).fetchone()
    after = conn.execute(
        "SELECT media_start_ts, actual_start_ts, start_ts, end_ts, duration_sec,"
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
    """The only place world time is converted to media time.

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


def _decode_ts_frame(path: Path, offset: float):
    """Indexless MPEG-TS fragment → decode from the lead keyframe and step to
    the target frame. Frame-accurate; never a POS_MSEC guess. ndarray or None.
    """
    import cv2
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 20.0
        target = max(0, int(round(max(0.0, offset) * fps)))
        frame = None
        for _ in range(target + 1):
            ok, f = cap.read()
            if not ok or f is None:
                break
            frame = f
        return frame
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
        frame = _decode_ts_frame(path, loc.media_offset)
        if frame is not None:
            return FrameResult(frame, "ok", "hls", None)
        # Live chunk not fetchable; authoritative MP4 exists after segment close.
        return FrameResult(None, "pending", "hls", _expected_close_ts(conn, source_id, t))

    # provider "none": gap or no_anchor — propagate honestly.
    return FrameResult(None, loc.reason, "none", None)


# ── shadow-mode invariant check ─────────────────────────────────────────────

@dataclass(frozen=True)
class RoundTrip:
    ok: bool
    detection_id: int
    status: str               # ok | sentinel | no_anchor | gap | world_mismatch | no_detection
    world_delta: float | None  # |resolved world - detection world|, the real invariant
    expected_offset: float
    resolved_offset: float | None
    provider: str
    alternate: bool           # resolved a valid but different segment (boundary duplicate)


def check_detection_round_trip(conn: sqlite3.Connection, video_dir: Path,
                               detection_id: int) -> RoundTrip:
    """Invariant: resolve(media->world(detection)) lands on a usable asset whose
    world time equals the detection's world time, within EPS.

    The invariant is on WORLD time, not offset equality: at contiguous segment
    boundaries the same wall instant exists in two files, so resolving to the
    adjacent segment (different offset) is correct. Sentinel rows (ts_offset<0,
    the "processed, nothing found" marker) and NULL media_epoch are reported as
    their own status, not resolver faults. No side effects.
    """
    row = conn.execute(
        "SELECT id, source_id, abs_ts, ts_offset AS media_offset"
        " FROM video_detections"
        " WHERE id=?",
        (detection_id,),
    ).fetchone()
    if not row:
        return RoundTrip(False, detection_id, "no_detection", None,
                         0.0, None, "none", False)

    expected = float(row["media_offset"])
    if expected < 0:  # sentinel marker, not a real frame
        return RoundTrip(True, detection_id, "sentinel", None,
                         expected, None, "none", False)
    world_t = float(row["abs_ts"])
    loc = resolve(conn, video_dir, row["source_id"], world_t)
    if loc.provider != "mp4" or loc.anchor is None or loc.media_offset is None:
        return RoundTrip(False, detection_id, loc.reason, None,
                         expected, loc.media_offset, loc.provider, False)
    resolved_world = loc.anchor.media_to_world(loc.media_offset)
    world_delta = abs(resolved_world - world_t)
    ok = world_delta < EPS
    alternate = abs(loc.media_offset - expected) > EPS
    return RoundTrip(ok, detection_id, "ok" if ok else "world_mismatch",
                     world_delta, expected, loc.media_offset, loc.provider, alternate)
