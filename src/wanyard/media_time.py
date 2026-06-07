"""Media-time resolver — the single boundary between world time and media time.

See docs/media-time-architecture.md.

World time = (source_id, t), t = unix UTC seconds. The only coordinate that
crosses module boundaries. Media time = (asset, offset), exists only inside this
module and the player/decoder.

This module is ADDITIVE. It reads existing columns (start_ts, actual_start_ts,
end_ts) but speaks only the new vocabulary. No caller is moved here yet; the
DB schema is unchanged. `media_epoch` maps to today's `actual_start_ts`,
`nominal_open_ts` to `start_ts`, `media_offset` to `ts_offset`.

Hard rule: media_epoch unknown -> provider "none", reason "no_anchor". Never
fall back to nominal_open_ts. Surfacing the unknown is the point.
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


def _none(reason: str, coverage: Coverage | None = None) -> MediaLocation:
    return MediaLocation(
        provider="none", url=None, media_offset=None,
        coverage=coverage, anchor=None, reason=reason,
    )


# ── recorded MP4 ────────────────────────────────────────────────────────────

def _resolve_recorded(conn: sqlite3.Connection, source_id: str,
                      t: float) -> MediaLocation | None:
    """Closed segment whose nominal bracket contains t. None = not recorded
    (caller should try live); a 'none' MediaLocation = recorded but unusable."""
    row = conn.execute(
        "SELECT id, path, start_ts, end_ts, actual_start_ts FROM segments"
        " WHERE source_id=? AND end_ts IS NOT NULL"
        "   AND start_ts<=? AND end_ts>=?"
        " ORDER BY start_ts DESC LIMIT 1",
        (source_id, t, t),
    ).fetchone()
    if not row:
        return None

    media_epoch = row["actual_start_ts"]
    if media_epoch is None:
        # Known unknown — do NOT guess with start_ts. Surface it.
        return _none("no_anchor")

    duration = float(row["end_ts"]) - float(row["start_ts"])
    anchor = Anchor("mp4", row["path"], float(media_epoch), duration)
    coverage = Coverage(anchor.media_epoch, anchor.media_epoch + duration)
    return MediaLocation(
        provider="mp4",
        url=f"/video/files/{row['path']}",
        media_offset=anchor.world_to_media(t),
        coverage=coverage,
        anchor=anchor,
        reason="recorded",
    )


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
    before = conn.execute(
        "SELECT actual_start_ts, start_ts, end_ts FROM segments"
        " WHERE source_id=? AND start_ts<=? ORDER BY start_ts DESC LIMIT 1",
        (source_id, t),
    ).fetchone()
    after = conn.execute(
        "SELECT actual_start_ts, start_ts, end_ts FROM segments"
        " WHERE source_id=? AND start_ts>? ORDER BY start_ts ASC LIMIT 1",
        (source_id, t),
    ).fetchone()

    def cov(row) -> Coverage | None:
        if not row:
            return None
        epoch = row["actual_start_ts"]
        if epoch is None:
            return None
        end = row["end_ts"]
        dur = (float(end) - float(row["start_ts"])) if end is not None else 0.0
        return Coverage(float(epoch), float(epoch) + dur)

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


# ── shadow-mode invariant check ─────────────────────────────────────────────

@dataclass(frozen=True)
class RoundTrip:
    ok: bool
    detection_id: int
    expected_offset: float
    resolved_offset: float | None
    provider: str
    reason: str

    @property
    def delta(self) -> float | None:
        if self.resolved_offset is None:
            return None
        return abs(self.resolved_offset - self.expected_offset)


def check_detection_round_trip(conn: sqlite3.Connection, video_dir: Path,
                               detection_id: int) -> RoundTrip:
    """Invariant: resolve(media->world(asset, media_offset)) lands back on the
    same asset+offset within EPS. Used in shadow mode / tests, no side effects.
    """
    row = conn.execute(
        "SELECT vd.id AS id, vd.ts_offset AS media_offset,"
        " s.source_id AS source_id, s.actual_start_ts AS media_epoch"
        " FROM video_detections vd JOIN segments s ON s.id=vd.segment_id"
        " WHERE vd.id=?",
        (detection_id,),
    ).fetchone()
    if not row:
        return RoundTrip(False, detection_id, 0.0, None, "none", "no_detection")

    media_epoch = row["media_epoch"]
    expected = float(row["media_offset"])
    if media_epoch is None:
        # Honest unknown: there is no world coordinate to round-trip from.
        return RoundTrip(False, detection_id, expected, None, "none", "no_anchor")

    world_t = float(media_epoch) + expected
    loc = resolve(conn, video_dir, row["source_id"], world_t)
    ok = (loc.provider == "mp4"
          and loc.media_offset is not None
          and abs(loc.media_offset - expected) < EPS)
    return RoundTrip(ok, detection_id, expected, loc.media_offset,
                     loc.provider, loc.reason)
