from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import math
import os
import socket
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse
import urllib.request

from . import sei
from .detection_settings import (
    COCO_CLASSES,
    DEFAULT_DETECTION_CLASSES,
    configured_detection_class_ids,
)
from .object_association import ByteTrackAssociator

LOG = logging.getLogger(__name__)

_MAX_SEGMENT_SECONDS = 600
# Open rows younger than this are skipped by crash salvage: the file may
# still be owned by a live ffmpeg after a watchdog thread restart.
_SALVAGE_MIN_IDLE_SECONDS = 30.0
# 1s live segments (was 2): halves the per-segment component of live latency.
# hls.js rides N segments back from the edge, so smaller segments = closer.
_LIVE_HLS_SEGMENT_SECONDS = 1
_LIVE_HLS_LIST_SIZE = _MAX_SEGMENT_SECONDS // _LIVE_HLS_SEGMENT_SECONDS
_LIVE_HLS_UNREFERENCED_RETENTION_SECONDS = (
    _LIVE_HLS_SEGMENT_SECONDS * (_LIVE_HLS_LIST_SIZE + 30)
)
_LIVE_HLS_STALE_PLAYLIST_SECONDS = _LIVE_HLS_UNREFERENCED_RETENTION_SECONDS
_SPRITE_FPS          = "1/5"
_SPRITE_W            = 160
_SPRITE_COLS         = 10
_SPRITE_ROWS         = 6
_EVENT_GAP_SECONDS   = 2.0    # detections within this gap = same event
_TRACKED_EVENT_GAP_SECONDS = 6.0
_ENCOUNTER_GAP_SECONDS = 6.0
_ENCOUNTER_GROUP_BASE_DISTANCE = 0.08
_ENCOUNTER_GROUP_SIZE_FACTOR = 0.65
_ENCOUNTER_MOTION_BASE_DISTANCE = 0.08
_ENCOUNTER_MOTION_SPEED_DISTANCE = 0.10
_ENCOUNTER_MAX_PREDICTED_DISTANCE = 0.36
_ENCOUNTER_MAX_AREA_RATIO = 8.0
_PROVISIONAL_GRACE_SECONDS = 3600.0
_OBJECT_TRACK_CENTER_DISTANCE = 0.045
_OBJECT_TRACK_AREA_RATIO = 3.0
_OBJECT_MIN_OBSERVATIONS = 2
_OBJECT_EXIT_GRACE_SECONDS = 15 * 60.0
_OBJECT_TRACK_LOOKBACK_SECONDS = 2 * 60 * 60.0
_CLASS_PRIORITY      = list(DEFAULT_DETECTION_CLASSES)
_NOTIFICATION_CONFIRMATION_STRATEGY = "yolo1280-crop640-960-v1"
_NOTIFICATION_CONFIRMATION_RETRY_SECONDS = 30.0
_NOTIFICATION_CONFIRMATION_TIMEOUT_SECONDS = 8.0
_CLOCK_ANCHOR_SCAN_SECONDS = 5.0
_CLOCK_ANCHOR_SCAN_FRAMES = 150
_CLOCK_ZERO_CONTAINER_PTS = "container_pts"
_CLOCK_ZERO_FIRST_FRAME = "first_frame"
_FRAME_CLOCK_VERSION = 1
_RECORD_RELAY_WAIT_SECONDS = 180.0
_RECORD_POLL_SECONDS = 5.0
_RECORD_RETRY_INITIAL_SECONDS = 5.0
_RECORD_RETRY_MAX_SECONDS = 300.0
_RECORD_SUCCESS_SECONDS = 30.0
_RECORD_EXCEPTION_RETRY_SECONDS = 30.0

_DDL = """
CREATE TABLE IF NOT EXISTS segments (
    id              INTEGER PRIMARY KEY,
    source_id       TEXT    NOT NULL,
    path            TEXT    NOT NULL UNIQUE,
    start_ts        REAL    NOT NULL,
    end_ts          REAL,
    media_epoch     REAL,        -- clock/Unix time at player media offset 0; the one anchor
    duration_sec    REAL,        -- playable media duration; private media metadata
    scanned_at      REAL,        -- wall time the detector finished scanning (NULL = pending)
    spritesheet     TEXT,
    webvtt          TEXT
);
CREATE INDEX IF NOT EXISTS seg_source_ts ON segments(source_id, start_ts);
CREATE INDEX IF NOT EXISTS seg_source_end_ts ON segments(source_id, end_ts, start_ts);
CREATE INDEX IF NOT EXISTS seg_source_media_epoch ON segments(source_id, media_epoch);

CREATE TABLE IF NOT EXISTS video_detections (
    id          INTEGER PRIMARY KEY,
    segment_id  INTEGER REFERENCES segments(id) ON DELETE CASCADE,
    source_id   TEXT    NOT NULL,
    abs_ts      REAL    NOT NULL,   -- decoded clock time of the tagged frame; the only time
    has_human   INTEGER NOT NULL DEFAULT 0,
    confidence  REAL    NOT NULL DEFAULT 0,
    boxes_json  TEXT,
    classes_json TEXT
);
CREATE INDEX IF NOT EXISTS vdet_seg ON video_detections(segment_id, abs_ts);
CREATE INDEX IF NOT EXISTS vdet_source_abs ON video_detections(source_id, abs_ts);

CREATE TABLE IF NOT EXISTS video_events (
    id          INTEGER PRIMARY KEY,
    segment_id  INTEGER REFERENCES segments(id) ON DELETE CASCADE,
    source_id   TEXT    NOT NULL,
    abs_ts      REAL    NOT NULL,
    class       TEXT    NOT NULL,
    start_off   REAL    NOT NULL,
    end_off     REAL    NOT NULL,
    confidence  REAL    NOT NULL DEFAULT 0,
    boxes_json  TEXT,
    event_type  TEXT    NOT NULL DEFAULT 'detection',
    track_id    TEXT
);
CREATE INDEX IF NOT EXISTS vevt_source_ts ON video_events(source_id, abs_ts);
CREATE INDEX IF NOT EXISTS vevt_class     ON video_events(class, abs_ts);
CREATE INDEX IF NOT EXISTS vevt_source_class_ts ON video_events(source_id, class, abs_ts);
CREATE INDEX IF NOT EXISTS vevt_ts        ON video_events(abs_ts);
CREATE INDEX IF NOT EXISTS vevt_seg       ON video_events(segment_id, class);

CREATE TABLE IF NOT EXISTS object_tracks (
    id               INTEGER PRIMARY KEY,
    source_id        TEXT    NOT NULL,
    class            TEXT    NOT NULL,
    cx               REAL    NOT NULL,
    cy               REAL    NOT NULL,
    area             REAL    NOT NULL,
    first_seen       REAL    NOT NULL,
    last_seen        REAL    NOT NULL,
    first_segment_id INTEGER,
    last_segment_id  INTEGER,
    first_start_off  REAL    NOT NULL DEFAULT 0,
    last_start_off   REAL    NOT NULL DEFAULT 0,
    last_end_off     REAL    NOT NULL DEFAULT 0,
    confidence       REAL    NOT NULL DEFAULT 0,
    observations     INTEGER NOT NULL DEFAULT 0,
    boxes_json       TEXT,
    active           INTEGER NOT NULL DEFAULT 1,
    state            TEXT    NOT NULL DEFAULT 'active',
    stationary_since REAL
);
CREATE INDEX IF NOT EXISTS otrk_active_source_class
    ON object_tracks(active, source_id, class, last_seen);
CREATE INDEX IF NOT EXISTS otrk_source_seen
    ON object_tracks(source_id, first_seen, last_seen);

CREATE TABLE IF NOT EXISTS object_events (
    id          INTEGER PRIMARY KEY,
    track_id    INTEGER REFERENCES object_tracks(id) ON DELETE CASCADE,
    segment_id  INTEGER REFERENCES segments(id) ON DELETE SET NULL,
    source_id   TEXT    NOT NULL,
    abs_ts      REAL    NOT NULL,
    display_ts  REAL    NOT NULL,
    class       TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,
    start_off   REAL    NOT NULL DEFAULT 0,
    end_off     REAL    NOT NULL DEFAULT 0,
    confidence  REAL    NOT NULL DEFAULT 0,
    boxes_json  TEXT
);
CREATE INDEX IF NOT EXISTS oevt_source_ts ON object_events(source_id, abs_ts);
CREATE INDEX IF NOT EXISTS oevt_source_display_ts ON object_events(source_id, display_ts);
CREATE INDEX IF NOT EXISTS oevt_class_ts ON object_events(class, abs_ts);
CREATE INDEX IF NOT EXISTS oevt_track ON object_events(track_id, abs_ts);
CREATE INDEX IF NOT EXISTS oevt_seg ON object_events(segment_id, class);

CREATE TABLE IF NOT EXISTS object_derivations (
    source_id    TEXT PRIMARY KEY,
    since        REAL,
    until        REAL,
    generated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS video_zones (
    id           INTEGER PRIMARY KEY,
    uid          TEXT,
    source_id    TEXT    NOT NULL,
    name         TEXT    NOT NULL,
    zone_type    TEXT    NOT NULL DEFAULT 'activity_area',
    polygon_json TEXT    NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   REAL    NOT NULL DEFAULT (unixepoch('now')),
    updated_at   REAL    NOT NULL DEFAULT (unixepoch('now'))
);
CREATE INDEX IF NOT EXISTS vzone_source_type
    ON video_zones(source_id, zone_type, enabled);
CREATE UNIQUE INDEX IF NOT EXISTS vzone_source_uid
    ON video_zones(source_id, uid);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visual_observations (
    event_ref       TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    observation_json TEXT NOT NULL,
    created_at      REAL NOT NULL DEFAULT (unixepoch('now')),
    PRIMARY KEY(event_ref, model, prompt_version)
);
CREATE INDEX IF NOT EXISTS vobs_created ON visual_observations(created_at);

CREATE TABLE IF NOT EXISTS notification_rules (
    id               INTEGER PRIMARY KEY,
    name             TEXT    NOT NULL,
    source_id        TEXT    NOT NULL,
    zone_ref         TEXT    NOT NULL DEFAULT 'whole_frame',
    classes_json     TEXT    NOT NULL DEFAULT '[]',
    enabled          INTEGER NOT NULL DEFAULT 1,
    cooldown_seconds INTEGER NOT NULL DEFAULT 60,
    created_at       REAL    NOT NULL DEFAULT (unixepoch('now')),
    updated_at       REAL    NOT NULL DEFAULT (unixepoch('now'))
);
CREATE INDEX IF NOT EXISTS nrule_source_zone
    ON notification_rules(source_id, zone_ref, enabled);

CREATE TABLE IF NOT EXISTS notification_events (
    id            INTEGER PRIMARY KEY,
    rule_id       INTEGER NOT NULL REFERENCES notification_rules(id) ON DELETE CASCADE,
    rule_name     TEXT    NOT NULL,
    source_id     TEXT    NOT NULL,
    zone_ref      TEXT    NOT NULL,
    event_ref     TEXT    NOT NULL,
    event_ts      REAL    NOT NULL,
    class         TEXT    NOT NULL,
    confidence    REAL    NOT NULL DEFAULT 0,
    title         TEXT    NOT NULL,
    body          TEXT    NOT NULL,
    thumb_url     TEXT,
    thumb_jpeg    BLOB,
    target_url    TEXT,
    read_at       REAL,
    dismissed_at  REAL,
    created_at    REAL    NOT NULL DEFAULT (unixepoch('now')),
    metadata_json TEXT    NOT NULL DEFAULT '{}',
    UNIQUE(rule_id, event_ref)
);
CREATE INDEX IF NOT EXISTS nevt_created
    ON notification_events(created_at DESC);
CREATE INDEX IF NOT EXISTS nevt_unread
    ON notification_events(read_at, dismissed_at, created_at);
CREATE INDEX IF NOT EXISTS nevt_rule_ts
    ON notification_events(rule_id, event_ts);

CREATE TABLE IF NOT EXISTS notification_confirmations (
    id               INTEGER PRIMARY KEY,
    strategy_version TEXT    NOT NULL,
    event_ref        TEXT    NOT NULL,
    source_id        TEXT    NOT NULL,
    event_ts         REAL    NOT NULL,
    class            TEXT    NOT NULL,
    status           TEXT    NOT NULL,
    confirmed        INTEGER NOT NULL DEFAULT 0,
    confidence       REAL    NOT NULL DEFAULT 0,
    reason           TEXT,
    boxes_json       TEXT,
    metadata_json    TEXT    NOT NULL DEFAULT '{}',
    created_at       REAL    NOT NULL DEFAULT (unixepoch('now')),
    updated_at       REAL    NOT NULL DEFAULT (unixepoch('now')),
    UNIQUE(strategy_version, event_ref)
);
CREATE INDEX IF NOT EXISTS nconf_status_updated
    ON notification_confirmations(status, updated_at);

-- Per-rule progress cursor over each event source table. Tracks the highest
-- row id already considered for notifications, keyed by insertion order (NOT
-- abs_ts) so late, backdated object_events — inserted after their segment
-- closes — are still picked up (their id is always above the cursor).
CREATE TABLE IF NOT EXISTS notification_cursor (
    rule_id   INTEGER NOT NULL REFERENCES notification_rules(id) ON DELETE CASCADE,
    kind      TEXT    NOT NULL,
    last_id   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (rule_id, kind)
);

CREATE TABLE IF NOT EXISTS notification_delivery_cursor (
    channel              TEXT PRIMARY KEY,
    destination_key      TEXT NOT NULL,
    last_notification_id INTEGER NOT NULL DEFAULT 0,
    updated_at           REAL NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    id               INTEGER PRIMARY KEY,
    notification_id  INTEGER NOT NULL
                     REFERENCES notification_events(id) ON DELETE CASCADE,
    channel          TEXT NOT NULL,
    destination_key  TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  REAL NOT NULL DEFAULT 0,
    last_error       TEXT,
    remote_id        TEXT,
    created_at       REAL NOT NULL DEFAULT (unixepoch('now')),
    updated_at       REAL NOT NULL DEFAULT (unixepoch('now')),
    delivered_at     REAL,
    UNIQUE(notification_id, channel, destination_key)
);
CREATE INDEX IF NOT EXISTS ndelivery_pending
    ON notification_deliveries(channel, destination_key, status, next_attempt_at);
"""



def _dominant_class(classes: list[str]) -> str:
    for c in _CLASS_PRIORITY:
        if c in classes:
            return c
    return classes[0] if classes else "unknown"


# ── clock time basis (see docs/media-time-architecture.md) ───────────────────
# Public media state uses one clock: decoded clock/Unix seconds. Historical
# recorder-open time (`start_ts`) and media offsets are private storage metadata.

def _row_value(row, key: str, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _seg_media_start(row) -> float | None:
    value = _row_value(row, "media_epoch")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _seg_duration(row) -> float | None:
    duration = _row_value(row, "duration_sec")
    if duration is not None:
        try:
            return max(0.0, float(duration))
        except (TypeError, ValueError):
            pass
    end_ts = _row_value(row, "end_ts")
    start_ts = _row_value(row, "start_ts")
    if end_ts is None or start_ts is None:
        return None
    try:
        return max(0.0, float(end_ts) - float(start_ts))
    except (TypeError, ValueError):
        return None


def _segment_media_epoch_sql(alias: str | None = None) -> str:
    p = f"{alias}." if alias else ""
    return f"{p}media_epoch"


def _worldize_event_row(r: dict) -> dict:
    """Public event row: abs_ts/display_ts are already universal time.

    The segment anchor for private offset math (thumbnails, clip export) is
    seg_media_epoch, carried straight from the segment's media_epoch. display_ts
    defaults to abs_ts when a row has no distinct display time.
    """
    if r.get("display_ts") is None and r.get("abs_ts") is not None:
        r["display_ts"] = r["abs_ts"]
    return r


class VideoSegmentDB:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._provisional_tracklet_cache: dict[int, tuple[int, list[dict]]] = {}
        self._provisional_encounter_cache: dict[int, tuple[int, list[dict]]] = {}
        self._provisional_tracklet_cache_lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # _DDL is the single source of truth: one pure schema, no migrations,
            # no backfill. A point in time is a point on video; media_epoch is the
            # single clock->player anchor and abs_ts the only public time.
            conn.executescript(_DDL)

    @contextmanager
    def _connect(self):
        # Closing context manager. `with sqlite3.connect(...) as c` only commits the
        # transaction on exit, it does NOT close the connection — every `with
        # self._connect()` was leaking a connection (3 fds in WAL) until the process
        # hit its open-file limit. Close it here.
        conn = sqlite3.connect(str(self._path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def open_segment(self, source_id: str, path: str, start_ts: float) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO segments(source_id, path, start_ts) VALUES(?,?,?)",
                (source_id, path, start_ts),
            )
            if cur.lastrowid:
                return cur.lastrowid
            return conn.execute(
                "SELECT id FROM segments WHERE path=?", (path,)
            ).fetchone()["id"]

    def close_segment(self, segment_id: int, end_ts: float,
                      spritesheet: str | None, webvtt: str | None) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT start_ts FROM segments WHERE id=?",
                (segment_id,),
            ).fetchone()
            duration = None
            if row:
                duration = max(0.0, float(end_ts) - float(row["start_ts"]))
            conn.execute(
                "UPDATE segments"
                " SET end_ts=?, duration_sec=?, spritesheet=?, webvtt=?"
                " WHERE id=?",
                (end_ts, duration, spritesheet, webvtt, segment_id),
            )

    def set_segment_media_start(self, segment_id: int, media_epoch: float) -> None:
        """Attach the SEI-derived world-time anchor for one segment."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE segments SET media_epoch=? WHERE id=?",
                (float(media_epoch), segment_id),
            )

    def mark_scanned(self, segment_id: int, scanned_at: float | None = None) -> None:
        """Record that the detector finished scanning a segment.

        Replaces the old sentinel detection row: a scanned segment with no rows
        simply has scanned_at set, so backfill never re-scans it.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE segments SET scanned_at=? WHERE id=?",
                (time.time() if scanned_at is None else float(scanned_at), segment_id),
            )

    def delete_segment(self, segment_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM segments WHERE id=?", (segment_id,))

    def open_segment_rows(self, source_id: str) -> list[dict]:
        """All open (end_ts IS NULL) segment rows for a source, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM segments"
                " WHERE source_id=? AND end_ts IS NULL ORDER BY start_ts",
                (source_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def open_live_segment(self, source_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM segments"
                " WHERE source_id=? AND end_ts IS NULL"
                " ORDER BY start_ts DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        return dict(row) if row else None

    def insert_live_detections(self, segment_id: int, source_id: str,
                               detections: list[dict]) -> int:
        if not detections:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO video_detections"
                "(segment_id, source_id, abs_ts, has_human,"
                " confidence, boxes_json, classes_json)"
                " VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        segment_id,
                        source_id,
                        float(d["abs_ts"]),
                        int(d["has_human"]),
                        float(d["confidence"]),
                        json.dumps(d["boxes"]) if d.get("boxes") else None,
                        json.dumps(d["classes"]) if d.get("classes") else None,
                    )
                    for d in detections
                ],
            )
        return len(detections)

    def replace_detections(self, segment_id: int, detections: list[dict]) -> None:
        """Store detections in clock time. Each carries abs_ts; nothing else times them."""
        with self._connect() as conn:
            seg = conn.execute(
                "SELECT source_id, media_epoch FROM segments WHERE id=?",
                (segment_id,),
            ).fetchone()
            if not seg:
                raise ValueError(f"unknown segment_id {segment_id}")
            conn.execute("DELETE FROM video_detections WHERE segment_id=?", (segment_id,))
            conn.executemany(
                "INSERT INTO video_detections"
                "(segment_id, source_id, abs_ts, has_human,"
                " confidence, boxes_json, classes_json)"
                " VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        segment_id,
                        seg["source_id"],
                        float(d["abs_ts"]),
                        int(d["has_human"]),
                        d["confidence"],
                        json.dumps(d["boxes"]) if d.get("boxes") else None,
                        json.dumps(d["classes"]) if d.get("classes") else None,
                    )
                    for d in detections
                ],
            )

    def get_segment(self, segment_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM segments WHERE id=?", (segment_id,)
            ).fetchone()
        return dict(row) if row else None

    def detections_for_segment(self, segment_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT vd.source_id, vd.abs_ts, vd.has_human, vd.confidence,"
                " vd.boxes_json, vd.classes_json, s.media_epoch AS media_epoch"
                " FROM video_detections vd JOIN segments s ON s.id=vd.segment_id"
                " WHERE vd.segment_id=? ORDER BY vd.abs_ts",
                (segment_id,),
            ).fetchall()
        return [{
            "source_id":   r["source_id"],
            "abs_ts":      r["abs_ts"],
            # offset into the file is derived on read, never stored
            "ts_offset":  (float(r["abs_ts"]) - float(r["media_epoch"])
                           if r["media_epoch"] is not None else None),
            "has_human":  bool(r["has_human"]),
            "confidence": r["confidence"],
            "boxes":   json.loads(r["boxes_json"])   if r["boxes_json"]   else [],
            "classes": json.loads(r["classes_json"]) if r["classes_json"] else [],
        } for r in rows]

    def detections_between(self, source_id: str, since: float, until: float) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT vd.segment_id, vd.source_id, vd.abs_ts,"
                " vd.has_human, vd.confidence, vd.boxes_json, vd.classes_json,"
                " s.media_epoch AS media_epoch"
                " FROM video_detections vd"
                " JOIN segments s ON s.id=vd.segment_id"
                " WHERE vd.source_id=?"
                " AND vd.abs_ts>=? AND vd.abs_ts<=?"
                " ORDER BY vd.abs_ts",
                (source_id, since, until),
            ).fetchall()

        detections = [{
            "segment_id":  r["segment_id"],
            "source_id":   r["source_id"],
            "abs_ts":      r["abs_ts"],
            "media_epoch": r["media_epoch"],
            "ts_offset":   (float(r["abs_ts"]) - float(r["media_epoch"])
                            if r["media_epoch"] is not None else None),
            "has_human":   bool(r["has_human"]),
            "confidence":  r["confidence"],
            "boxes":       json.loads(r["boxes_json"])   if r["boxes_json"]   else [],
            "classes":     json.loads(r["classes_json"]) if r["classes_json"] else [],
        } for r in rows]
        detections.sort(key=lambda d: d["abs_ts"])
        return detections

    def list_zones(self, source_id: str | None = None,
                   zone_type: str | None = None) -> list[dict]:
        where, params = ["1"], []
        if source_id and source_id != "all":
            where.append("source_id=?")
            params.append(source_id)
        if zone_type:
            where.append("zone_type=?")
            params.append(zone_type)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM video_zones"
                f" WHERE {' AND '.join(where)}"
                " ORDER BY source_id, id",
                params,
            ).fetchall()
        zones: list[dict] = []
        for row in rows:
            try:
                polygon = json.loads(row["polygon_json"])
            except (TypeError, json.JSONDecodeError):
                polygon = []
            zones.append({
                "id": row["id"],
                "uid": row["uid"],
                "source_id": row["source_id"],
                "name": row["name"],
                "type": row["zone_type"],
                "polygon": polygon,
                "enabled": bool(row["enabled"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })
        return zones

    def replace_zones(self, source_id: str, zones: list[dict]) -> list[dict]:
        if not source_id or source_id == "all":
            raise ValueError("source_id is required")
        now = time.time()
        with self._connect() as conn:
            existing_by_id = {
                row["id"]: row["uid"]
                for row in conn.execute(
                    "SELECT id, uid FROM video_zones WHERE source_id=?", (source_id,)
                ).fetchall()
            }
            sanitized = []
            seen_uids: set[str] = set()
            for z in zones:
                item = _sanitize_zone(source_id, z)
                uid = _normalize_zone_uid(z.get("uid"))
                try:
                    zone_id = int(z.get("id"))
                except (TypeError, ValueError):
                    zone_id = None
                if not uid and zone_id in existing_by_id:
                    uid = _normalize_zone_uid(existing_by_id[zone_id])
                if not uid or uid in seen_uids:
                    uid = _new_zone_uid()
                seen_uids.add(uid)
                item["uid"] = uid
                sanitized.append(item)
            conn.execute("DELETE FROM video_zones WHERE source_id=?", (source_id,))
            conn.executemany(
                "INSERT INTO video_zones"
                " (uid, source_id, name, zone_type, polygon_json, enabled, created_at, updated_at)"
                " VALUES(:uid,:source_id,:name,:zone_type,:polygon_json,:enabled,:created_at,:updated_at)",
                [
                    {
                        **z,
                        "created_at": now,
                        "updated_at": now,
                    }
                    for z in sanitized
                ],
            )
        return self.list_zones(source_id)

    def save_zone(self, source_id: str, zone: dict,
                  zone_uid: str | None = None) -> dict:
        """Create or update one camera area without rewriting its neighbours."""
        if not source_id or source_id == "all":
            raise ValueError("source_id is required")
        item = _sanitize_zone(source_id, zone)
        now = time.time()
        with self._connect() as conn:
            if zone_uid is None:
                uid = _new_zone_uid()
                conn.execute(
                    "INSERT INTO video_zones"
                    " (uid, source_id, name, zone_type, polygon_json, enabled, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        uid, source_id, item["name"], item["zone_type"],
                        item["polygon_json"], item["enabled"], now, now,
                    ),
                )
            else:
                uid = _normalize_zone_uid(zone_uid)
                if not uid:
                    raise ValueError("invalid zone uid")
                changed = conn.execute(
                    "UPDATE video_zones SET name=?, zone_type=?, polygon_json=?,"
                    " enabled=?, updated_at=? WHERE source_id=? AND uid=?",
                    (
                        item["name"], item["zone_type"], item["polygon_json"],
                        item["enabled"], now, source_id, uid,
                    ),
                ).rowcount
                if not changed:
                    raise LookupError("area not found")
        return next(z for z in self.list_zones(source_id) if z["uid"] == uid)

    def delete_zone(self, source_id: str, zone_uid: str) -> bool:
        if not source_id or source_id == "all":
            raise ValueError("source_id is required")
        uid = _normalize_zone_uid(zone_uid)
        if not uid:
            raise ValueError("invalid zone uid")
        with self._connect() as conn:
            return bool(conn.execute(
                "DELETE FROM video_zones WHERE source_id=? AND uid=?",
                (source_id, uid),
            ).rowcount)

    def list_notification_rules(self, source_id: str | None = None) -> list[dict]:
        where, params = ["1"], []
        if source_id and source_id != "all":
            where.append("source_id=?")
            params.append(source_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notification_rules"
                f" WHERE {' AND '.join(where)}"
                " ORDER BY source_id, enabled DESC, name COLLATE NOCASE, id",
                params,
            ).fetchall()
        return [_notification_rule_from_row(row) for row in rows]

    def create_notification_rule(self, data: dict) -> dict:
        rule = _sanitize_notification_rule(data)
        now = time.time()
        with self._connect() as conn:
            _validate_notification_zone_ref(conn, rule["source_id"], rule["zone_ref"])
            cur = conn.execute(
                "INSERT INTO notification_rules"
                " (name, source_id, zone_ref, classes_json, enabled,"
                " cooldown_seconds, created_at, updated_at)"
                " VALUES(:name,:source_id,:zone_ref,:classes_json,:enabled,"
                " :cooldown_seconds,:created_at,:updated_at)",
                {
                    **rule,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            rule_id = int(cur.lastrowid)
            # Seed the cursors at the current max id in the same transaction, so
            # a detection arriving between now and the first materialize tick is
            # caught (its id will be above the seed) rather than seeded over.
            for kind in _NOTIFICATION_KINDS:
                _notification_cursor_set(
                    conn, rule_id, kind, _notification_kind_max_id(conn, kind)
                )
            row = conn.execute(
                "SELECT * FROM notification_rules WHERE id=?", (rule_id,)
            ).fetchone()
        return _notification_rule_from_row(row)

    def update_notification_rule(self, rule_id: int, data: dict) -> dict | None:
        if not isinstance(data, dict):
            raise ValueError("rule must be an object")
        with self._connect() as conn:
            current = conn.execute(
                "SELECT * FROM notification_rules WHERE id=?", (rule_id,)
            ).fetchone()
            if current is None:
                return None
            merged = _notification_rule_from_row(current)
            merged.update(data)
            rule = _sanitize_notification_rule(merged)
            _validate_notification_zone_ref(conn, rule["source_id"], rule["zone_ref"])
            conn.execute(
                "UPDATE notification_rules SET"
                " name=:name, source_id=:source_id, zone_ref=:zone_ref,"
                " classes_json=:classes_json, enabled=:enabled,"
                " cooldown_seconds=:cooldown_seconds, updated_at=:updated_at"
                " WHERE id=:id",
                {
                    **rule,
                    "id": rule_id,
                    "updated_at": time.time(),
                },
            )
            row = conn.execute(
                "SELECT * FROM notification_rules WHERE id=?", (rule_id,)
            ).fetchone()
        return _notification_rule_from_row(row)

    def delete_notification_rule(self, rule_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM notification_rules WHERE id=?", (rule_id,)
            )
        return cur.rowcount > 0

    def materialize_notifications(self, batch_per_kind: int = 2000) -> int:
        """Turn newly-seen detections into notifications.

        Progress is tracked per (rule, source-kind) by row id in
        notification_cursor — keyed on insertion order, not abs_ts, so late
        backdated object_events are never skipped. The cursor advances over
        every scanned row regardless of whether it emits, so a stretch of
        rejected/cooldown-skipped events can never freeze the pipeline.
        """
        inserted = 0
        with self._connect() as conn:
            rules = conn.execute(
                "SELECT * FROM notification_rules WHERE enabled=1 ORDER BY id"
            ).fetchall()
        for rule_row in rules:
            inserted += self._materialize_rule(
                _notification_rule_from_row(rule_row), batch_per_kind
            )
        return inserted

    def _materialize_rule(self, rule: dict, batch_per_kind: int) -> int:
        cooldown = max(0, int(rule["cooldown_seconds"]))
        with self._connect() as conn:
            last_row = conn.execute(
                "SELECT MAX(event_ts) FROM notification_events WHERE rule_id=?",
                (rule["id"],),
            ).fetchone()
        # cooldown anchor — only ever advanced by an actual emission
        cursor_ts = float(last_row[0]) if last_row and last_row[0] is not None else None
        inserted = 0
        blocked: set[str] = set()  # kinds held back this call by a retryable error
        while True:
            # Fetch a batch from every not-yet-blocked kind. Do NOT advance the
            # cursor yet — an event whose confirmation errors (YOLO down, frame
            # unavailable) must be retried, so the cursor cannot pass it.
            batch: list[dict] = []
            bounds: dict[str, tuple[int, int]] = {}  # kind -> (start_id, max_id)
            with self._connect() as conn:
                for kind in _NOTIFICATION_KINDS:
                    if kind in blocked:
                        continue
                    start_id = _notification_cursor_get(conn, rule["id"], kind)
                    events, max_id = _notification_events_after(
                        conn, rule, kind, start_id, batch_per_kind
                    )
                    bounds[kind] = (start_id, max_id)
                    batch.extend(events)
            if not bounds:
                break
            # Cooldown is a time concept, so order the merged batch by abs_ts.
            unresolved: dict[str, int] = {}  # kind -> lowest id still pending
            for event in sorted(batch, key=lambda e: e["abs_ts"]):
                event_ts = float(event["abs_ts"])
                if cursor_ts is not None and event_ts < cursor_ts + cooldown:
                    continue  # terminal: deliberately suppressed
                event_ref = _notification_event_ref(event)
                confirmation = self._ensure_notification_confirmation(event, event_ref)
                if confirmation.get("confirmed"):
                    with self._connect() as conn:
                        row = _build_notification_event(
                            conn, rule, {**event, "_confirmation": confirmation}, time.time()
                        )
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO notification_events"
                            " (rule_id, rule_name, source_id, zone_ref, event_ref,"
                            " event_ts, class, confidence, title, body, thumb_url,"
                            " thumb_jpeg, target_url, created_at, metadata_json)"
                            " VALUES(:rule_id,:rule_name,:source_id,:zone_ref,:event_ref,"
                            " :event_ts,:class,:confidence,:title,:body,:thumb_url,"
                            " :thumb_jpeg,:target_url,:created_at,:metadata_json)",
                            row,
                        )
                    cursor_ts = event_ts
                    if cur.rowcount:
                        inserted += 1
                elif str(confirmation.get("status")) == "rejected":
                    pass  # terminal: confirmed-not-a-match
                else:
                    # Retryable (error / frame unavailable) — hold the cursor at
                    # this id so the next pass re-examines it.
                    k = event["_kind"]
                    eid = int(event["id"])
                    unresolved[k] = min(unresolved.get(k, eid), eid)
            # Advance each kind's cursor to the highest id with no pending event
            # before it; a blocked kind stops being fetched again this call.
            advanced = False
            with self._connect() as conn:
                for kind, (start_id, max_id) in bounds.items():
                    target = max_id
                    if kind in unresolved:
                        target = min(target, unresolved[kind] - 1)
                        blocked.add(kind)
                    if target > start_id:
                        _notification_cursor_set(conn, rule["id"], kind, target)
                        advanced = True
            if not advanced:
                break
        return inserted

    def _ensure_notification_confirmation(self, event: dict, event_ref: str) -> dict:
        if not _notification_confirmation_enabled():
            return {
                "status": "confirmed",
                "confirmed": True,
                "confidence": float(event.get("confidence") or 0.0),
                "reason": "confirmation_disabled",
                "strategy_version": _NOTIFICATION_CONFIRMATION_STRATEGY,
                "metadata": {},
            }

        now = time.time()
        cached = self._get_notification_confirmation(event_ref)
        if cached:
            status = str(cached.get("status") or "")
            age = now - float(cached.get("updated_at") or 0.0)
            if status in {"confirmed", "rejected"}:
                return cached
            if age < _NOTIFICATION_CONFIRMATION_RETRY_SECONDS:
                return cached

        result = _confirm_notification_with_yolo(event, event_ref)
        self._save_notification_confirmation(event, event_ref, result)
        return result

    def _get_notification_confirmation(self, event_ref: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notification_confirmations"
                " WHERE strategy_version=? AND event_ref=?",
                (_NOTIFICATION_CONFIRMATION_STRATEGY, event_ref),
            ).fetchone()
        return _notification_confirmation_from_row(row)

    def _save_notification_confirmation(
        self,
        event: dict,
        event_ref: str,
        result: dict,
    ) -> None:
        now = time.time()
        metadata = result.get("metadata") or {}
        boxes = result.get("boxes") or []
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO notification_confirmations"
                " (strategy_version, event_ref, source_id, event_ts, class,"
                " status, confirmed, confidence, reason, boxes_json,"
                " metadata_json, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(strategy_version, event_ref) DO UPDATE SET"
                " source_id=excluded.source_id,"
                " event_ts=excluded.event_ts,"
                " class=excluded.class,"
                " status=excluded.status,"
                " confirmed=excluded.confirmed,"
                " confidence=excluded.confidence,"
                " reason=excluded.reason,"
                " boxes_json=excluded.boxes_json,"
                " metadata_json=excluded.metadata_json,"
                " updated_at=excluded.updated_at",
                (
                    _NOTIFICATION_CONFIRMATION_STRATEGY,
                    event_ref,
                    str(event.get("source_id") or ""),
                    float(event.get("abs_ts") or 0.0),
                    str(event.get("class") or ""),
                    str(result.get("status") or "error"),
                    1 if result.get("confirmed") else 0,
                    float(result.get("confidence") or 0.0),
                    str(result.get("reason") or ""),
                    json.dumps(boxes, separators=(",", ":")),
                    json.dumps(metadata, separators=(",", ":")),
                    now,
                    now,
                ),
            )

    def list_notifications(
        self,
        limit: int = 30,
        unread_only: bool = False,
        include_dismissed: bool = False,
    ) -> list[dict]:
        where, params = ["n.event_ts>=r.created_at"], []
        if unread_only:
            where.append("n.read_at IS NULL")
        if not include_dismissed:
            where.append("n.dismissed_at IS NULL")
        limit = max(1, min(200, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT n.* FROM notification_events n"
                " JOIN notification_rules r ON r.id=n.rule_id"
                f" WHERE {' AND '.join(where)}"
                " ORDER BY n.event_ts DESC, n.id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [_notification_event_from_row(row) for row in rows]

    def unread_notification_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM notification_events n"
                " JOIN notification_rules r ON r.id=n.rule_id"
                " WHERE n.event_ts>=r.created_at"
                " AND n.read_at IS NULL AND n.dismissed_at IS NULL"
            ).fetchone()
        return int(row[0] if row else 0)

    def mark_notification_read(self, notification_id: int) -> dict | None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE notification_events SET read_at=COALESCE(read_at, ?)"
                " WHERE id=?",
                (time.time(), notification_id),
            )
            row = conn.execute(
                "SELECT * FROM notification_events WHERE id=?", (notification_id,)
            ).fetchone()
        return _notification_event_from_row(row) if row else None

    def mark_all_notifications_read(self) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE notification_events SET read_at=COALESCE(read_at, ?)"
                " WHERE read_at IS NULL AND dismissed_at IS NULL",
                (time.time(),),
            )
        return cur.rowcount

    def get_notification_thumb(self, notification_id: int) -> bytes | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT thumb_jpeg FROM notification_events WHERE id=?",
                (notification_id,),
            ).fetchone()
        return bytes(row["thumb_jpeg"]) if row and row["thumb_jpeg"] else None

    def configure_notification_delivery(
        self,
        channel: str,
        destination_key: str,
        *,
        force_seed: bool = False,
    ) -> int:
        """Prepare a delivery channel without replaying its existing backlog."""
        channel = str(channel).strip()
        destination_key = str(destination_key).strip()
        if not channel or not destination_key:
            raise ValueError("channel and destination are required")
        now = time.time()
        with self._connect() as conn:
            current = conn.execute(
                "SELECT destination_key, last_notification_id"
                " FROM notification_delivery_cursor WHERE channel=?",
                (channel,),
            ).fetchone()
            if (
                current is not None
                and current["destination_key"] == destination_key
                and not force_seed
            ):
                return int(current["last_notification_id"])
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM notification_events"
            ).fetchone()
            seed = int(row[0] if row else 0)
            conn.execute(
                "INSERT INTO notification_delivery_cursor"
                " (channel, destination_key, last_notification_id, updated_at)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(channel) DO UPDATE SET"
                " destination_key=excluded.destination_key,"
                " last_notification_id=excluded.last_notification_id,"
                " updated_at=excluded.updated_at",
                (channel, destination_key, seed, now),
            )
            conn.execute(
                "UPDATE notification_deliveries SET"
                " status='superseded', updated_at=?"
                " WHERE channel=? AND destination_key<>?"
                " AND status IN ('pending','retry')",
                (now, channel, destination_key),
            )
        return seed

    def enqueue_notification_deliveries(
        self,
        channel: str,
        destination_key: str,
        *,
        now: float | None = None,
        max_event_age: float = 300.0,
        limit: int = 200,
    ) -> int:
        """Queue new durable notifications and advance the channel cursor."""
        now = time.time() if now is None else float(now)
        limit = max(1, min(2000, int(limit)))
        cutoff = now - max(0.0, float(max_event_age))
        inserted = 0
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT destination_key, last_notification_id"
                " FROM notification_delivery_cursor WHERE channel=?",
                (channel,),
            ).fetchone()
            if cursor is None or cursor["destination_key"] != destination_key:
                return 0
            rows = conn.execute(
                "SELECT id, event_ts FROM notification_events"
                " WHERE id>? ORDER BY id ASC LIMIT ?",
                (int(cursor["last_notification_id"]), limit),
            ).fetchall()
            for row in rows:
                if float(row["event_ts"]) < cutoff:
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO notification_deliveries"
                    " (notification_id, channel, destination_key, status,"
                    " next_attempt_at, created_at, updated_at)"
                    " VALUES(?,?,?,'pending',?,?,?)",
                    (
                        int(row["id"]),
                        channel,
                        destination_key,
                        now,
                        now,
                        now,
                    ),
                )
                inserted += int(cur.rowcount > 0)
            if rows:
                conn.execute(
                    "UPDATE notification_delivery_cursor SET"
                    " last_notification_id=?, updated_at=? WHERE channel=?",
                    (int(rows[-1]["id"]), now, channel),
                )
        return inserted

    def pending_notification_deliveries(
        self,
        channel: str,
        destination_key: str,
        *,
        now: float | None = None,
        limit: int = 20,
    ) -> list[dict]:
        now = time.time() if now is None else float(now)
        limit = max(1, min(200, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT n.*, d.id AS delivery_id, d.attempts AS delivery_attempts"
                " FROM notification_deliveries d"
                " JOIN notification_events n ON n.id=d.notification_id"
                " WHERE d.channel=? AND d.destination_key=?"
                " AND d.status IN ('pending','retry')"
                " AND d.next_attempt_at<=?"
                " ORDER BY d.id ASC LIMIT ?",
                (channel, destination_key, now, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = _notification_event_from_row(row)
            item["_delivery_id"] = int(row["delivery_id"])
            item["_delivery_attempts"] = int(row["delivery_attempts"])
            result.append(item)
        return result

    def complete_notification_delivery(
        self,
        delivery_id: int,
        remote_id: str | None,
        *,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else float(now)
        with self._connect() as conn:
            conn.execute(
                "UPDATE notification_deliveries SET status='delivered',"
                " attempts=attempts+1, remote_id=?, last_error=NULL,"
                " delivered_at=?, updated_at=? WHERE id=?",
                (remote_id, now, now, int(delivery_id)),
            )

    def fail_notification_delivery(
        self,
        delivery_id: int,
        error: str,
        *,
        retryable: bool,
        retry_after: float | None = None,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else float(now)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempts FROM notification_deliveries WHERE id=?",
                (int(delivery_id),),
            ).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"]) + 1
            terminal = not retryable or attempts >= 8
            if terminal:
                status, next_attempt = "failed", 0.0
            else:
                delay = (
                    max(1.0, float(retry_after))
                    if retry_after is not None
                    else min(900.0, 5.0 * (3 ** (attempts - 1)))
                )
                status, next_attempt = "retry", now + delay
            conn.execute(
                "UPDATE notification_deliveries SET status=?, attempts=?,"
                " next_attempt_at=?, last_error=?, updated_at=? WHERE id=?",
                (
                    status,
                    attempts,
                    next_attempt,
                    str(error)[:500],
                    now,
                    int(delivery_id),
                ),
            )

    def notification_delivery_status(
        self,
        channel: str,
        destination_key: str | None = None,
    ) -> dict:
        where = ["channel=?"]
        params: list = [channel]
        if destination_key:
            where.append("destination_key=?")
            params.append(destination_key)
        clause = " AND ".join(where)
        with self._connect() as conn:
            counts = conn.execute(
                "SELECT status, COUNT(*) AS n FROM notification_deliveries"
                f" WHERE {clause} GROUP BY status",
                params,
            ).fetchall()
            latest = conn.execute(
                "SELECT delivered_at FROM notification_deliveries"
                f" WHERE {clause} AND status='delivered'"
                " ORDER BY delivered_at DESC LIMIT 1",
                params,
            ).fetchone()
            error = conn.execute(
                "SELECT last_error, updated_at FROM notification_deliveries"
                f" WHERE {clause} AND last_error IS NOT NULL"
                " ORDER BY updated_at DESC LIMIT 1",
                params,
            ).fetchone()
        return {
            "counts": {str(row["status"]): int(row["n"]) for row in counts},
            "last_delivered_at": (
                float(latest["delivered_at"]) if latest else None
            ),
            # A later successful delivery is the best health signal. Do not
            # leave Settings red forever because an older transient failed.
            "last_error": (
                str(error["last_error"])
                if error and (
                    latest is None
                    or float(error["updated_at"])
                    > float(latest["delivered_at"])
                )
                else None
            ),
            "last_error_at": (
                float(error["updated_at"])
                if error and (
                    latest is None
                    or float(error["updated_at"])
                    > float(latest["delivered_at"])
                )
                else None
            ),
        }

    def prune_orphan_notifications(self) -> dict[str, int]:
        """Remove notifications whose referenced clock time has no footage.

        Retention normally removes notifications alongside their segments, but
        maintenance migrations can delete historical segment rows directly.
        Those leftovers have dead thumbnails and targets that cannot resolve.
        Coverage deliberately uses the segment row rather than detection ids:
        backfill can replace detection ids while the MP4 remains playable.
        """
        coverage = (
            "SELECT 1 FROM segments s"
            " WHERE s.source_id={table}.source_id"
            " AND {table}.event_ts >= COALESCE(s.media_epoch, s.start_ts)"
            " AND (s.end_ts IS NULL OR {table}.event_ts <= CASE"
            "   WHEN s.media_epoch IS NOT NULL AND s.duration_sec IS NOT NULL"
            "     THEN s.media_epoch + s.duration_sec + 0.5"
            "   ELSE s.end_ts + 0.5 END)"
        )
        with self._connect() as conn:
            events = conn.execute(
                "DELETE FROM notification_events"
                f" WHERE NOT EXISTS ({coverage.format(table='notification_events')})"
            ).rowcount
            confirmations = conn.execute(
                "DELETE FROM notification_confirmations"
                f" WHERE NOT EXISTS ({coverage.format(table='notification_confirmations')})"
            ).rowcount
        return {"events": events, "confirmations": confirmations}

    def activity_areas(self, source_id: str) -> list[list[dict]]:
        return [
            z["polygon"] for z in self.list_zones(source_id)
            if z["type"] in {"activity_area", "vehicle_event"}
            and z["enabled"] and len(z["polygon"]) >= 3
        ]

    def exclusion_areas(self, source_id: str) -> list[list[dict]]:
        return [
            z["polygon"] for z in self.list_zones(source_id)
            if z["type"] == "exclusion_area"
            and z["enabled"] and len(z["polygon"]) >= 3
        ]

    def has_activity_areas(self, source_id: str | None = None) -> bool:
        return any(
            z["type"] in {"activity_area", "vehicle_event"}
            and z["enabled"] and len(z["polygon"]) >= 3
            for z in self.list_zones(source_id)
        )

    def has_exclusion_areas(self, source_id: str | None = None) -> bool:
        return any(
            z["type"] == "exclusion_area"
            and z["enabled"] and len(z["polygon"]) >= 3
            for z in self.list_zones(source_id)
        )

    def filter_events_with_zone_policy(
        self,
        events: list[dict],
        source_id: str | None,
        polygons: list[list[dict]],
    ) -> list[dict]:
        if source_id and source_id != "all":
            return _filter_with_zone_policy(
                events, polygons, self.exclusion_areas(source_id)
            )
        exclusion_cache: dict[str, list[list[dict]]] = {}
        filtered: list[dict] = []
        for event in events:
            event_source = str(event.get("source_id") or "")
            if event_source not in exclusion_cache:
                exclusion_cache[event_source] = (
                    self.exclusion_areas(event_source) if event_source else []
                )
            if _event_allowed_by_zone_policy(
                event, polygons, exclusion_cache[event_source]
            ):
                filtered.append(event)
        return filtered

    def filter_events_by_areas(self, events: list[dict]) -> list[dict]:
        area_cache: dict[str, list[list[dict]]] = {}
        filtered: list[dict] = []
        for event in events:
            source_id = event.get("source_id")
            if not source_id:
                filtered.append(event)
                continue
            if source_id not in area_cache:
                area_cache[source_id] = self.activity_areas(source_id)
            exclusions = self.exclusion_areas(source_id)
            if _event_allowed_by_zone_policy(
                event, area_cache[source_id], exclusions
            ):
                filtered.append(event)
        return filtered

    def zone_polygons(self, source_id: str | None,
                      zone_id) -> list[list[dict]]:
        """Polygons to filter by. zone=none means whole frame."""
        if _zone_filter_disabled(zone_id):
            return []
        if source_id and source_id != "all" and zone_id is not None and str(zone_id) != "all":
            try:
                z_id = int(zone_id)
            except (TypeError, ValueError):
                return self.activity_areas(source_id)
            for z in self.list_zones(source_id):
                if (
                    z["id"] == z_id
                    and z["type"] in {"activity_area", "vehicle_event"}
                    and z["enabled"] and len(z["polygon"]) >= 3
                ):
                    return [z["polygon"]]
        return self.activity_areas(source_id) if source_id else []

    def object_events_available(
        self,
        source_id: str | None = None,
        since: float | None = None,
        until: float | None = None,
    ) -> bool:
        where, params = ["1"], []
        if source_id and source_id != "all":
            where.append("source_id=?")
            params.append(source_id)
        if since is not None:
            where.append("display_ts>=?")
            params.append(since)
        if until is not None:
            where.append("display_ts<=?")
            params.append(until)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT 1 FROM object_events WHERE {' AND '.join(where)} LIMIT 1",
                params,
            ).fetchone()
        return row is not None

    def mark_object_derivation(
        self,
        source_id: str,
        since: float | None = None,
        until: float | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO object_derivations(source_id, since, until, generated_at)"
                " VALUES(?,?,?,?)",
                (source_id, since, until, time.time()),
            )

    def insert_object_events(self, events: list[dict]) -> None:
        if not events:
            return
        rows = [
            {
                **event,
                "display_ts": event.get("display_ts", event["abs_ts"]),
            }
            for event in events
        ]
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO object_events"
                "(track_id, segment_id, source_id, abs_ts, display_ts, class, event_type,"
                " start_off, end_off, confidence, boxes_json)"
                " VALUES(:track_id,:segment_id,:source_id,:abs_ts,:display_ts,:class,:event_type,"
                " :start_off,:end_off,:confidence,:boxes_json)",
                rows,
            )

    def list_object_events(self, source_id: str | None = None, cls: str | None = None,
                           date: str | None = None, limit: int = 100,
                           since: float | None = None,
                           until: float | None = None,
                           zone_id=None) -> list[dict]:
        where, params = ["1"], []
        if source_id and source_id != "all":
            where.append("e.source_id=?"); params.append(source_id)
        if cls and cls != "all":
            where.append("e.class=?"); params.append(cls)
        if since is not None:
            where.append("e.display_ts>=?"); params.append(since)
        if until is not None:
            where.append("e.display_ts<=?"); params.append(until)
        if date:
            import calendar
            from datetime import date as ddate
            d = ddate.fromisoformat(date)
            lo = calendar.timegm(d.timetuple()) - 86400
            hi = lo + 3 * 86400
            where.append("e.display_ts BETWEEN ? AND ?")
            params += [lo, hi]
        polygons = self.zone_polygons(source_id, zone_id)
        query_limit = limit
        if (polygons or self.has_exclusion_areas(source_id)) and limit < 100000:
            query_limit = max(limit * 20, 200)
        sql = (
            "SELECT e.*, s.path as seg_path, s.spritesheet,"
            " s.media_epoch as seg_media_epoch"
            " FROM object_events e LEFT JOIN segments s ON s.id=e.segment_id"
            f" WHERE {' AND '.join(where)}"
            " ORDER BY e.display_ts DESC LIMIT ?"
        )
        params.append(query_limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        events = self.filter_events_with_zone_policy(
            [_worldize_event_row(dict(r)) for r in rows], source_id, polygons)[:limit]
        return [_public_object_event(r) for r in events]

    def nearest_object_events(self, around: float, source_id: str | None = None,
                              classes: list[str] | None = None,
                              limit: int = 20, zone_id=None) -> list[dict]:
        if classes and len(classes) > 1:
            rows: list[dict] = []
            for cls in classes:
                rows.extend(self.nearest_object_events(around, source_id, [cls], limit, zone_id))
            by_id = {r["id"]: r for r in rows}
            rows = list(by_id.values())
            rows.sort(key=lambda r: (
                abs(float(r.get("display_ts", r["abs_ts"])) - around),
                float(r.get("display_ts", r["abs_ts"])),
            ))
            return rows[:limit]

        where, params = ["1"], []
        if source_id and source_id != "all":
            where.append("e.source_id=?"); params.append(source_id)
        if classes:
            placeholders = ",".join("?" for _ in classes)
            where.append(f"e.class IN ({placeholders})")
            params.extend(classes)
        base = " AND ".join(where)
        select = (
            "SELECT e.*, s.path as seg_path, s.spritesheet,"
            " s.media_epoch as seg_media_epoch"
            " FROM object_events e LEFT JOIN segments s ON s.id=e.segment_id"
            f" WHERE {base}"
        )
        polygons = self.zone_polygons(source_id, zone_id)
        query_limit = max(limit * 20, 200) if (polygons or self.has_exclusion_areas(source_id)) else limit
        with self._connect() as conn:
            before = conn.execute(
                f"{select} AND e.display_ts<=? ORDER BY e.display_ts DESC LIMIT ?",
                (*params, around, query_limit),
            ).fetchall()
            after = conn.execute(
                f"{select} AND e.display_ts>? ORDER BY e.display_ts ASC LIMIT ?",
                (*params, around, query_limit),
            ).fetchall()
        rows = self.filter_events_with_zone_policy(
            [_worldize_event_row(dict(r)) for r in before]
            + [_worldize_event_row(dict(r)) for r in after], source_id, polygons)
        rows.sort(key=lambda r: (
            abs(float(r.get("display_ts", r["abs_ts"])) - around),
            float(r.get("display_ts", r["abs_ts"])),
        ))
        return [_public_object_event(r) for r in rows[:limit]]

    def insert_events(self, events: list[dict]) -> None:
        rows = [
            {
                **event,
                "event_type": event.get("event_type", "detection"),
                "track_id": event.get("track_id"),
            }
            for event in events
        ]
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO video_events"
                "(segment_id, source_id, abs_ts, class, start_off, end_off,"
                " confidence, boxes_json, event_type, track_id)"
                " VALUES(:segment_id,:source_id,:abs_ts,:class,:start_off,:end_off,"
                " :confidence,:boxes_json,:event_type,:track_id)",
                rows,
            )

    def insert_encounter_events(self, events: list[dict]) -> None:
        """Persist group encounters, continuing matches over segment edges."""
        if not events:
            return
        used_prior_ids: set[int] = set()
        with self._connect() as conn:
            for event in sorted(events, key=lambda row: row["abs_ts"]):
                state = _encounter_state(event.get("track_id"))
                first_box = event.get("_encounter_first_box")
                first_ts = float(
                    event.get("_encounter_first_abs_ts", event["abs_ts"])
                )
                best: tuple[float, sqlite3.Row, dict] | None = None
                candidates = conn.execute(
                    "SELECT e.* FROM video_events e"
                    " WHERE e.source_id=? AND e.class=?"
                    " AND e.event_type='detection' AND e.segment_id<>?"
                    " ORDER BY e.id DESC LIMIT 100",
                    (
                        event["source_id"],
                        event["class"],
                        event["segment_id"],
                    ),
                ).fetchall()
                for candidate in candidates:
                    candidate_id = int(candidate["id"])
                    if candidate_id in used_prior_ids:
                        continue
                    previous = _encounter_state(candidate["track_id"])
                    if not previous or not state or not first_box:
                        continue
                    gap = first_ts - float(previous["last_abs_ts"])
                    if gap < 0 or gap > _ENCOUNTER_GAP_SECONDS:
                        continue
                    previous_box = previous.get("last_box")
                    if not isinstance(previous_box, dict):
                        continue
                    previous_cx, previous_cy = _box_center(previous_box)
                    current_cx, current_cy = _box_center(first_box)
                    predicted_x = (
                        previous_cx + float(previous.get("vx", 0.0)) * gap
                    )
                    predicted_y = (
                        previous_cy + float(previous.get("vy", 0.0)) * gap
                    )
                    distance = _center_distance(
                        current_cx, current_cy, predicted_x, predicted_y
                    )
                    shared_tokens = (
                        set(previous.get("tracker_ids") or [])
                        & set(event.get("_encounter_tracker_ids") or [])
                    )
                    max_distance = (
                        0.65
                        if shared_tokens
                        else min(
                            _ENCOUNTER_MAX_PREDICTED_DISTANCE,
                            _ENCOUNTER_MOTION_BASE_DISTANCE
                            + _ENCOUNTER_MOTION_SPEED_DISTANCE * gap,
                        )
                    )
                    if distance > max_distance:
                        continue
                    score = distance - (1.0 if shared_tokens else 0.0)
                    if best is None or score < best[0]:
                        best = (score, candidate, previous)

                if best is not None:
                    _, candidate, previous = best
                    used_prior_ids.add(int(candidate["id"]))
                    if state:
                        state["tracker_ids"] = sorted(
                            set(previous.get("tracker_ids") or [])
                            | set(state.get("tracker_ids") or [])
                        )
                    merged_boxes = _merge_encounter_area_boxes(
                        candidate["boxes_json"],
                        event.get("boxes_json"),
                    )
                    conn.execute(
                        "UPDATE video_events SET confidence=?, boxes_json=?, track_id=?"
                        " WHERE id=?",
                        (
                            max(
                                float(candidate["confidence"]),
                                float(event.get("confidence", 0.0)),
                            ),
                            merged_boxes,
                            json.dumps(state, separators=(",", ":")),
                            candidate["id"],
                        ),
                    )
                    continue

                row = {
                    **event,
                    "event_type": event.get("event_type", "detection"),
                    "track_id": event.get("track_id"),
                }
                conn.execute(
                    "INSERT INTO video_events"
                    "(segment_id, source_id, abs_ts, class, start_off, end_off,"
                    " confidence, boxes_json, event_type, track_id)"
                    " VALUES(:segment_id,:source_id,:abs_ts,:class,:start_off,:end_off,"
                    " :confidence,:boxes_json,:event_type,:track_id)",
                    row,
                )

    def track_object_events(self, segment: dict, tracklets: list[dict]) -> list[dict]:
        source_id = segment["source_id"]
        media_start = _seg_media_start(segment)
        if media_start is None:
            return []
        duration = _seg_duration(segment) or 0.0
        seg_start = media_start
        seg_end = media_start + duration
        output: list[dict] = []

        with self._connect() as conn:
            active = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM object_tracks"
                    " WHERE active=1 AND source_id=? AND last_seen>=?"
                    " ORDER BY last_seen DESC",
                    (source_id, seg_start - _OBJECT_TRACK_LOOKBACK_SECONDS),
                ).fetchall()
            ]
            used_track_ids: set[int] = set()

            for tracklet in sorted(tracklets, key=lambda e: e["abs_ts"]):
                first_event = {
                    **tracklet,
                    "boxes_json": tracklet.get(
                        "first_boxes_json", tracklet.get("boxes_json")
                    ),
                }
                first_box = _event_box(first_event)
                if not first_box:
                    continue
                last_event = {
                    **tracklet,
                    "boxes_json": tracklet.get(
                        "last_boxes_json", tracklet.get("boxes_json")
                    ),
                }
                last_box = _event_box(last_event) or first_box
                tracker_id = _box_tracker_id(first_box)
                first_cx, first_cy = _box_center(first_box)
                first_area = _box_area(first_box)
                last_cx, last_cy = _box_center(last_box)
                last_area = _box_area(last_box)
                best: dict | None = None
                best_dist = _OBJECT_TRACK_CENTER_DISTANCE
                for track in active:
                    if int(track["id"]) in used_track_ids:
                        continue
                    if track["class"] != tracklet["class"]:
                        continue
                    previous_tracker_id = _event_tracker_id(track)
                    if tracker_id and previous_tracker_id:
                        if tracker_id == previous_tracker_id:
                            best = track
                            break
                        continue
                    if not _area_compatible(first_area, float(track["area"])):
                        continue
                    dist = _center_distance(
                        first_cx,
                        first_cy,
                        float(track["cx"]),
                        float(track["cy"]),
                    )
                    if dist <= best_dist:
                        best = track
                        best_dist = dist

                first_seen = float(
                    tracklet.get("track_first_abs_ts", tracklet["abs_ts"])
                )
                last_seen = float(
                    tracklet.get(
                        "track_last_abs_ts",
                        float(tracklet["abs_ts"]) + max(
                            0.0,
                            float(tracklet["end_off"])
                            - float(tracklet["start_off"]),
                        ),
                    )
                )
                first_start_off = float(
                    tracklet.get("track_first_off", tracklet["start_off"])
                )
                state_boxes_json = tracklet.get(
                    "last_boxes_json", tracklet["boxes_json"]
                )
                if best:
                    track_id = int(best["id"])
                    used_track_ids.add(track_id)
                    observations = int(best["observations"]) + int(tracklet.get("observations", 1))
                    conn.execute(
                        "UPDATE object_tracks"
                        " SET cx=?, cy=?, area=?, last_seen=?, last_segment_id=?,"
                        " last_start_off=?, last_end_off=?, confidence=?, observations=?,"
                        " boxes_json=?, active=1, state='active'"
                        " WHERE id=?",
                        (
                            last_cx, last_cy, last_area, last_seen,
                            tracklet["segment_id"],
                            tracklet["start_off"], tracklet["end_off"],
                            tracklet["confidence"], observations,
                            state_boxes_json, track_id,
                        ),
                    )
                    best.update({
                        "cx": last_cx,
                        "cy": last_cy,
                        "area": last_area,
                        "last_seen": last_seen,
                        "last_segment_id": tracklet["segment_id"],
                        "last_start_off": tracklet["start_off"],
                        "last_end_off": tracklet["end_off"],
                        "confidence": tracklet["confidence"],
                        "observations": observations,
                        "boxes_json": state_boxes_json,
                    })
                else:
                    cur = conn.execute(
                        "INSERT INTO object_tracks"
                        "(source_id, class, cx, cy, area, first_seen, last_seen,"
                        " first_segment_id, last_segment_id, first_start_off,"
                        " last_start_off, last_end_off, confidence, observations,"
                        " boxes_json, active, state, stationary_since)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'active',?)",
                        (
                            source_id, tracklet["class"],
                            last_cx, last_cy, last_area,
                            first_seen, last_seen, tracklet["segment_id"],
                            tracklet["segment_id"], first_start_off,
                            tracklet["start_off"], tracklet["end_off"],
                            tracklet["confidence"], int(tracklet.get("observations", 1)),
                            state_boxes_json, first_seen,
                        ),
                    )
                    track_id = int(cur.lastrowid)
                    used_track_ids.add(track_id)
                    active.append({
                        "id": track_id,
                        "source_id": source_id,
                        "class": tracklet["class"],
                        "cx": last_cx,
                        "cy": last_cy,
                        "area": last_area,
                        "first_seen": first_seen,
                        "last_seen": last_seen,
                        "first_segment_id": tracklet["segment_id"],
                        "last_segment_id": tracklet["segment_id"],
                        "first_start_off": first_start_off,
                        "last_start_off": tracklet["start_off"],
                        "last_end_off": tracklet["end_off"],
                        "confidence": tracklet["confidence"],
                        "observations": int(tracklet.get("observations", 1)),
                        "boxes_json": state_boxes_json,
                        "active": 1,
                        "state": "active",
                        "stationary_since": first_seen,
                    })
                    output.append({
                        **tracklet,
                        "event_type": "appeared",
                        "display_ts": tracklet["abs_ts"],
                        "track_id": track_id,
                    })

            stale_before = seg_end - _OBJECT_EXIT_GRACE_SECONDS
            stale = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM object_tracks"
                    " WHERE active=1 AND source_id=? AND last_seen<?",
                    (source_id, stale_before),
                ).fetchall()
            ]
            for track in stale:
                track_id = int(track["id"])
                if track_id in used_track_ids:
                    continue
                conn.execute(
                    "UPDATE object_tracks SET active=0, state='gone' WHERE id=?",
                    (track_id,),
                )
                output.append({
                    "track_id": track_id,
                    "segment_id": track["last_segment_id"],
                    "source_id": track["source_id"],
                    "abs_ts": track["last_seen"],
                    "display_ts": track["last_seen"],
                    "class": track["class"],
                    "event_type": "disappeared",
                    "start_off": track["last_start_off"],
                    "end_off": track["last_end_off"],
                    "confidence": track["confidence"],
                    "boxes_json": track["boxes_json"],
                })

        return output

    def list_events(self, source_id: str | None = None, cls: str | None = None,
                    date: str | None = None, limit: int = 100,
                    since: float | None = None,
                    until: float | None = None,
                    zone_id=None) -> list[dict]:
        if self.object_events_available(source_id, since, until):
            return self.list_object_events(source_id, cls, date, limit, since, until, zone_id)

        where, params = ["1"], []
        if source_id and source_id != "all":
            where.append("e.source_id=?"); params.append(source_id)
        if cls and cls != "all":
            where.append("e.class=?"); params.append(cls)
        if since is not None:
            where.append("e.abs_ts>=?"); params.append(since)
        if until is not None:
            where.append("e.abs_ts<=?"); params.append(until)
        if date:
            # date is YYYY-MM-DD local; filter by Unix day boundary approximately
            import calendar
            from datetime import date as ddate
            d = ddate.fromisoformat(date)
            # rough UTC bounds (±1 day for timezone safety, client filters)
            lo = calendar.timegm(d.timetuple()) - 86400
            hi = lo + 3 * 86400
            where.append("e.abs_ts BETWEEN ? AND ?")
            params += [lo, hi]
        polygons = self.zone_polygons(source_id, zone_id)
        query_limit = limit
        if (polygons or self.has_exclusion_areas(source_id)) and limit < 100000:
            query_limit = max(limit * 20, 200)
        sql = (
            "SELECT e.*, s.path as seg_path, s.spritesheet,"
            " s.media_epoch as seg_media_epoch"
            " FROM video_events e JOIN segments s ON s.id=e.segment_id"
            f" WHERE {' AND '.join(where)}"
            " ORDER BY e.abs_ts DESC LIMIT ?"
        )
        params.append(query_limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return self.filter_events_with_zone_policy(
            [_worldize_event_row(dict(r)) for r in rows], source_id, polygons)[:limit]

    def list_detection_events(
        self,
        source_id: str,
        classes: list[str] | None = None,
        limit: int = 25,
        before: float | None = None,
    ) -> list[dict]:
        """Newest persisted group encounters for the thumbnail wall."""
        where, params = [
            "e.source_id=?",
            "e.event_type!='disappeared'",
        ], [source_id]
        if classes:
            placeholders = ",".join("?" for _ in classes)
            where.append(f"e.class IN ({placeholders})")
            params.extend(classes)
        if before is not None:
            where.append("e.abs_ts<?")
            params.append(before)
        params.append(max(1, int(limit)))
        sql = (
            "SELECT e.*, s.path as seg_path, s.spritesheet,"
            " s.media_epoch as seg_media_epoch"
            " FROM video_events e JOIN segments s ON s.id=e.segment_id"
            f" WHERE {' AND '.join(where)}"
            " ORDER BY e.abs_ts DESC, e.id DESC LIMIT ?"
        )
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        return [_worldize_event_row(row) for row in rows]

    def search_detection_events(
        self,
        source_ids: list[str],
        classes: list[str] | None,
        since: float,
        until: float,
        limit: int = 48,
    ) -> list[dict]:
        """Search finalized encounter evidence inside an absolute time window."""
        source_ids = list(dict.fromkeys(str(value) for value in source_ids if value))
        if not source_ids:
            return []
        where = ["e.event_type!='disappeared'", "e.abs_ts>=?", "e.abs_ts<?"]
        params: list[object] = [float(since), float(until)]
        source_marks = ",".join("?" for _ in source_ids)
        where.append(f"e.source_id IN ({source_marks})")
        params.extend(source_ids)
        if classes:
            class_marks = ",".join("?" for _ in classes)
            where.append(f"e.class IN ({class_marks})")
            params.extend(classes)
        params.append(max(1, min(200, int(limit))))
        sql = (
            "SELECT e.*, s.path as seg_path, s.spritesheet,"
            " s.media_epoch as seg_media_epoch"
            " FROM video_events e JOIN segments s ON s.id=e.segment_id"
            f" WHERE {' AND '.join(where)}"
            " ORDER BY e.abs_ts DESC, e.id DESC LIMIT ?"
        )
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        return [_worldize_event_row(row) for row in rows]

    def visual_observation(
        self, event_ref: str, model: str, prompt_version: str
    ) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT observation_json, created_at FROM visual_observations"
                " WHERE event_ref=? AND model=? AND prompt_version=?",
                (event_ref, model, prompt_version),
            ).fetchone()
        if row is None:
            return None
        try:
            observation = json.loads(row["observation_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(observation, dict):
            return None
        return {**observation, "created_at": float(row["created_at"])}

    def store_visual_observation(
        self,
        event_ref: str,
        model: str,
        prompt_version: str,
        observation: dict,
    ) -> None:
        encoded = json.dumps(observation, separators=(",", ":"), sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO visual_observations"
                "(event_ref, model, prompt_version, observation_json, created_at)"
                " VALUES(?,?,?,?,unixepoch('now'))"
                " ON CONFLICT(event_ref, model, prompt_version) DO UPDATE SET"
                " observation_json=excluded.observation_json,"
                " created_at=excluded.created_at",
                (event_ref, model, prompt_version, encoded),
            )

    def detection_class_counts(
        self,
        source_id: str | None = None,
        polygons: list[list[dict]] | None = None,
        include_provisional: bool = True,
        exclusions: list[list[dict]] | None = None,
        classes: list[str] | None = None,
    ) -> dict[str, int]:
        """Class counts for the detection wall, optionally inside any polygon.

        ``polygons=None`` means whole frame. A non-empty list applies union
        semantics using the same box-centre rule as timeline activity areas.
        """
        table = "video_events"
        where, params = ["event_type!='disappeared'"], []
        if source_id and source_id != "all":
            where.append("source_id=?")
            params.append(source_id)
        if classes:
            placeholders = ",".join("?" for _ in classes)
            where.append(f"class IN ({placeholders})")
            params.extend(classes)
        predicate = " AND ".join(where) if where else "1"
        counts: dict[str, int] = {}
        with self._connect() as conn:
            if polygons is None and not exclusions:
                rows = conn.execute(
                    f"SELECT class, COUNT(*) AS n FROM {table}"
                    f" WHERE {predicate} GROUP BY class",
                    params,
                ).fetchall()
                counts = {row["class"]: int(row["n"]) for row in rows}
            else:
                rows = conn.execute(
                    f"SELECT class, boxes_json FROM {table}"
                    f" WHERE {predicate}",
                    params,
                ).fetchall()
                for event in _filter_with_zone_policy(
                    [dict(row) for row in rows], polygons or [], exclusions or []
                ):
                    cls = str(event["class"])
                    counts[cls] = counts.get(cls, 0) + 1

        if include_provisional:
            provisional = self.provisional_detection_events(
                source_id, zone_id="none"
            )
            if classes:
                wanted = set(classes)
                provisional = [
                    event for event in provisional
                    if event.get("class") in wanted
                ]
            if polygons is not None or exclusions:
                provisional = _filter_with_zone_policy(
                    provisional, polygons or [], exclusions or []
                )
            for event in provisional:
                cls = str(event["class"])
                counts[cls] = counts.get(cls, 0) + 1
        return counts

    def nearest_events(self, around: float, source_id: str | None = None,
                       classes: list[str] | None = None,
                       limit: int = 20, zone_id=None) -> list[dict]:
        if self.object_events_available(source_id):
            return self.nearest_object_events(around, source_id, classes, limit, zone_id)

        if classes and len(classes) > 1:
            rows: list[dict] = []
            for cls in classes:
                rows.extend(self.nearest_events(around, source_id, [cls], limit, zone_id))
            by_id = {r["id"]: r for r in rows}
            rows = list(by_id.values())
            rows.sort(key=lambda r: (abs(r["abs_ts"] - around), r["abs_ts"]))
            return rows[:limit]

        where, params = ["1"], []
        if source_id and source_id != "all":
            where.append("e.source_id=?"); params.append(source_id)
        if classes:
            placeholders = ",".join("?" for _ in classes)
            where.append(f"e.class IN ({placeholders})")
            params.extend(classes)
        base = " AND ".join(where)
        select = (
            "SELECT e.*, s.path as seg_path, s.spritesheet,"
            " s.media_epoch as seg_media_epoch"
            " FROM video_events e JOIN segments s ON s.id=e.segment_id"
            f" WHERE {base}"
        )
        polygons = self.zone_polygons(source_id, zone_id)
        query_limit = max(limit * 20, 200) if (polygons or self.has_exclusion_areas(source_id)) else limit
        with self._connect() as conn:
            before = conn.execute(
                f"{select} AND e.abs_ts<=? ORDER BY e.abs_ts DESC LIMIT ?",
                (*params, around, query_limit),
            ).fetchall()
            after = conn.execute(
                f"{select} AND e.abs_ts>? ORDER BY e.abs_ts ASC LIMIT ?",
                (*params, around, query_limit),
            ).fetchall()
        rows = self.filter_events_with_zone_policy(
            [_worldize_event_row(dict(r)) for r in before]
            + [_worldize_event_row(dict(r)) for r in after], source_id, polygons)
        rows.sort(key=lambda r: (abs(r["abs_ts"] - around), r["abs_ts"]))
        return rows[:limit]

    def notification_class_for_ref(self, event_ref: str) -> str | None:
        """Class of the notification that references ``event_ref``.

        Detection refs (``d:<id>``) resolve with NULL class — a per-frame
        detection row can hold several classes at once — but the notification
        knows which class fired. Thumb cropping needs it: on a frame with a
        high-confidence car and the notified bird, classless selection crops
        the car.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT class FROM notification_events"
                " WHERE event_ref=? ORDER BY id DESC LIMIT 1",
                (event_ref,),
            ).fetchone()
        return row["class"] if row and row["class"] else None

    def event_like_for_notification_ref(self, event_ref: str,
                                        tolerance: float = 2.0) -> dict | None:
        """Best current stand-in for a notification's dead event ref.

        Backfill's replace_detections re-inserts a segment's rows under new
        ids, so a notification's ``d:<id>`` ref can stop resolving while an
        equivalent detection (same source, same instant, fresh id) exists.
        Re-resolve by the notification's own (source_id, event_ts): nearest
        detection within ``tolerance`` seconds, shaped like
        :meth:`get_event_with_segment`.
        """
        with self._connect() as conn:
            notif = conn.execute(
                "SELECT source_id, event_ts FROM notification_events"
                " WHERE event_ref=? ORDER BY id DESC LIMIT 1",
                (event_ref,),
            ).fetchone()
            if notif is None:
                return None
            row = conn.execute(
                "SELECT vd.boxes_json AS boxes_json, vd.confidence AS confidence,"
                " vd.abs_ts AS abs_ts,"
                " s.path AS seg_path,"
                " s.media_epoch AS seg_media_epoch,"
                " s.end_ts AS seg_end_ts,"
                " NULL AS class"
                " FROM video_detections vd JOIN segments s ON s.id=vd.segment_id"
                " WHERE vd.source_id=? AND vd.abs_ts BETWEEN ? AND ?"
                " ORDER BY ABS(vd.abs_ts - ?) LIMIT 1",
                (notif["source_id"],
                 float(notif["event_ts"]) - tolerance,
                 float(notif["event_ts"]) + tolerance,
                 float(notif["event_ts"])),
            ).fetchone()
        return dict(row) if row else None

    def get_event_with_segment(self, event_id) -> dict | None:
        raw_id = str(event_id)
        if raw_id.startswith("d:"):
            # Per-frame detection: the thumb seek is (abs_ts - media_epoch) into
            # the MP4, so report seg_media_epoch/abs_ts such that
            # (abs_ts - seg_media_epoch) lands exactly on the tagged frame.
            try:
                det_id = int(raw_id[2:])
            except ValueError:
                return None
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT vd.boxes_json AS boxes_json, vd.confidence AS confidence,"
                    " vd.abs_ts AS abs_ts,"
                    " s.path AS seg_path,"
                    " s.media_epoch AS seg_media_epoch,"
                    " s.end_ts AS seg_end_ts,"
                    " NULL AS class"
                    " FROM video_detections vd JOIN segments s ON s.id=vd.segment_id"
                    " WHERE vd.id=?",
                    (det_id,),
                ).fetchone()
            return dict(row) if row else None
        if raw_id.startswith("o:"):
            try:
                object_event_id = int(raw_id[2:])
            except ValueError:
                return None
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT e.*, s.path as seg_path,"
                    " s.media_epoch as seg_media_epoch, s.end_ts as seg_end_ts"
                    " FROM object_events e JOIN segments s ON s.id=e.segment_id"
                    " WHERE e.id=?",
                    (object_event_id,),
                ).fetchone()
                if not row:
                    return None
                event = _worldize_event_row(dict(row))
                event["thumbnail_abs_ts"] = _object_event_thumbnail_abs_ts(
                    conn, event
                )
            return event

        if raw_id.startswith("p:"):
            try:
                _prefix, segment_id_raw, cls, start_off_raw = raw_id.split(":", 3)
                segment_id = int(segment_id_raw)
                start_off = float(start_off_raw)
            except ValueError:
                return None
            with self._connect() as conn:
                seg_row = conn.execute(
                    "SELECT * FROM segments WHERE id=?",
                    (segment_id,),
                ).fetchone()
            if not seg_row:
                return None
            seg = dict(seg_row)
            for row in self._provisional_tracklets_for_segment(seg):
                if row.get("class") != cls:
                    continue
                if round(float(row.get("start_off", -1.0)), 1) != round(start_off, 1):
                    continue
                row["id"] = raw_id
                row["provisional"] = True
                row["seg_path"] = seg["path"]
                row["spritesheet"] = seg.get("spritesheet")
                row["seg_media_epoch"] = seg.get("media_epoch")
                row["seg_end_ts"] = seg.get("end_ts")
                return _worldize_event_row(row)
            return None

        if raw_id.startswith("g:"):
            try:
                _prefix, segment_id_raw, cls, start_off_raw = raw_id.split(":", 3)
                segment_id = int(segment_id_raw)
                start_off = float(start_off_raw)
            except ValueError:
                return None
            with self._connect() as conn:
                seg_row = conn.execute(
                    "SELECT * FROM segments WHERE id=?",
                    (segment_id,),
                ).fetchone()
            if not seg_row:
                return None
            seg = dict(seg_row)
            for row in self._provisional_encounters_for_segment(seg):
                if row.get("class") != cls:
                    continue
                if round(float(row.get("start_off", -1.0)), 1) != round(start_off, 1):
                    continue
                row["id"] = raw_id
                row["provisional"] = True
                row["seg_path"] = seg["path"]
                row["spritesheet"] = seg.get("spritesheet")
                row["seg_media_epoch"] = seg.get("media_epoch")
                row["seg_end_ts"] = seg.get("end_ts")
                return _worldize_event_row(row)
            return None

        try:
            legacy_event_id = int(raw_id)
        except (TypeError, ValueError):
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT e.*, s.path as seg_path,"
                " s.media_epoch as seg_media_epoch, s.end_ts as seg_end_ts"
                " FROM video_events e JOIN segments s ON s.id=e.segment_id"
                " WHERE e.id=?",
                (legacy_event_id,),
            ).fetchone()
        return _worldize_event_row(dict(row)) if row else None

    def get_setting(self, key: str, default=None):
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        val = row[0]
        try:
            return float(val) if '.' in val else int(val)
        except (ValueError, TypeError):
            return val

    def set_setting(self, key: str, value) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES(?,?)",
                         (key, str(value)))

    def get_all_settings(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
        return {r[0]: r[1] for r in rows}

    def class_counts(self, source_id: str | None = None,
                     include_provisional: bool = True,
                     zone_id=None) -> dict[str, int]:
        table = "object_events" if self.object_events_available(source_id) else "video_events"
        episode_filter = "event_type='appeared'" if table == "object_events" else "1"
        polygons = self.zone_polygons(source_id, zone_id)
        exclusions_active = self.has_exclusion_areas(source_id)
        if polygons or exclusions_active:
            where, params = [episode_filter], []
            if source_id and source_id != "all":
                where.append("source_id=?")
                params.append(source_id)
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT source_id, class, boxes_json FROM {table}"
                    f" WHERE {' AND '.join(where)}",
                    params,
                ).fetchall()
            counts: dict[str, int] = {}
            for event in self.filter_events_with_zone_policy(
                [dict(r) for r in rows], source_id, polygons
            ):
                counts[event["class"]] = counts.get(event["class"], 0) + 1
        else:
            with self._connect() as conn:
                if source_id and source_id != "all":
                    rows = conn.execute(
                        f"SELECT class, COUNT(*) as n FROM {table}"
                        f" WHERE {episode_filter} AND source_id=? GROUP BY class",
                        (source_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"SELECT class, COUNT(*) as n FROM {table}"
                        f" WHERE {episode_filter} GROUP BY class"
                    ).fetchall()
            counts = {r["class"]: r["n"] for r in rows}
        if include_provisional:
            for evt in self.provisional_events(source_id, zone_id=zone_id):
                counts[evt["class"]] = counts.get(evt["class"], 0) + 1
        return counts

    def activity_summary(self, source_id: str | None = None,
                         since: float | None = None,
                         until: float | None = None,
                         zone_id=None) -> dict:
        table = (
            "object_events"
            if self.object_events_available(source_id, since, until)
            else "video_events"
        )
        where, params = ["1"], []
        if table == "object_events":
            where.append("event_type='appeared'")
        if source_id and source_id != "all":
            where.append("source_id=?"); params.append(source_id)
        if since is not None:
            where.append(("display_ts>=?" if table == "object_events" else "abs_ts>=?"))
            params.append(since)
        if until is not None:
            where.append(("display_ts<?" if table == "object_events" else "abs_ts<?"))
            params.append(until)
        polygons = self.zone_polygons(source_id, zone_id)
        exclusions_active = self.has_exclusion_areas(source_id)
        if polygons or exclusions_active:
            sql = (
                f"SELECT source_id, class, boxes_json FROM {table}"
                f" WHERE {' AND '.join(where)}"
            )
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
            classes: dict[str, int] = {}
            for event in self.filter_events_with_zone_policy(
                [dict(r) for r in rows], source_id, polygons
            ):
                classes[event["class"]] = classes.get(event["class"], 0) + 1
        else:
            sql = (
                f"SELECT class, COUNT(*) as n FROM {table}"
                f" WHERE {' AND '.join(where)}"
                " GROUP BY class"
            )
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
            classes = {r["class"]: r["n"] for r in rows}
        for evt in self.provisional_events(source_id, since, zone_id=zone_id):
            if until is not None and evt["abs_ts"] >= until:
                continue
            classes[evt["class"]] = classes.get(evt["class"], 0) + 1
        return {"total": sum(classes.values()), "classes": classes}

    def segment_bounds(self, source_id: str | None = None) -> dict | None:
        epoch = _segment_media_epoch_sql()
        now = f"{time.time():.3f}"
        coverage_end = (
            f"(CASE WHEN end_ts IS NOT NULL"
            f" THEN ({epoch} + COALESCE(duration_sec, end_ts - start_ts))"
            f" ELSE {now} END)"
        )
        where, params = [f"{epoch} IS NOT NULL"], []
        if source_id and source_id != "all":
            where.append("source_id=?"); params.append(source_id)
        sql = f"SELECT MIN({epoch}) AS from_ts, MAX({coverage_end}) AS to_ts FROM segments"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        if not row or row["from_ts"] is None or row["to_ts"] is None:
            return None
        return {"from": row["from_ts"], "to": row["to_ts"]}

    def list_segments(self, source_id: str | None = None,
                      since: float | None = None,
                      until: float | None = None) -> list[dict]:
        epoch = _segment_media_epoch_sql()
        now = f"{time.time():.3f}"
        coverage_end = (
            f"(CASE WHEN end_ts IS NOT NULL"
            f" THEN ({epoch} + COALESCE(duration_sec, end_ts - start_ts))"
            f" ELSE {now} END)"
        )
        where, params = [f"{epoch} IS NOT NULL"], []
        if source_id and source_id != "all":
            where.append("source_id=?"); params.append(source_id)
        if since is not None:
            where.append(f"{coverage_end}>?")
            params.append(float(since))
        if until is not None:
            where.append(f"{epoch}<?")
            params.append(float(until))
        sql = "SELECT * FROM segments"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {epoch} DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def segments_overlapping(self, source_id: str | None,
                             start_ts: float, end_ts: float) -> list[dict]:
        epoch = _segment_media_epoch_sql()
        coverage_end = f"({epoch} + COALESCE(duration_sec, end_ts - start_ts))"
        where, params = [
            "end_ts IS NOT NULL",
            f"{epoch} IS NOT NULL",
            f"{coverage_end}>?",
            f"{epoch}<?",
        ], [start_ts, end_ts]
        if source_id and source_id != "all":
            where.append("source_id=?"); params.append(source_id)
        sql = (
            "SELECT * FROM segments"
            f" WHERE {' AND '.join(where)}"
            f" ORDER BY {epoch}"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def segment_at(self, source_id: str | None, ts: float, *,
                   exact: bool = True) -> dict | None:
        epoch = _segment_media_epoch_sql()
        coverage_end = f"({epoch} + COALESCE(duration_sec, end_ts - start_ts))"
        where, params = [
            "end_ts IS NOT NULL",
            f"{epoch} IS NOT NULL",
            f"{epoch}<=?",
            f"{coverage_end}>?",
        ], [ts, ts]
        if source_id and source_id != "all":
            where.append("source_id=?"); params.append(source_id)
        sql = (
            "SELECT * FROM segments"
            f" WHERE {' AND '.join(where)}"
            f" ORDER BY {epoch} DESC LIMIT 1"
        )
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            if not row and not exact:
                where2, params2 = [
                    "end_ts IS NOT NULL",
                    f"{epoch} IS NOT NULL",
                    f"{coverage_end}<=?",
                ], [ts]
                if source_id and source_id != "all":
                    where2.append("source_id=?"); params2.append(source_id)
                row = conn.execute(
                    "SELECT * FROM segments"
                    f" WHERE {' AND '.join(where2)}"
                    f" ORDER BY {coverage_end} DESC LIMIT 1",
                    params2,
                ).fetchone()
        return dict(row) if row else None

    def provisional_events(self, source_id: str | None = None,
                           since: float | None = None,
                           zone_id=None) -> list[dict]:
        segs = self._provisional_segments(source_id, since)
        if not source_id or source_id == "all":
            qualifying_ids = {int(seg["id"]) for seg in segs}
            with self._provisional_tracklet_cache_lock:
                for cache in (
                    self._provisional_tracklet_cache,
                    self._provisional_encounter_cache,
                ):
                    for seg_id in list(cache):
                        if seg_id not in qualifying_ids:
                            cache.pop(seg_id, None)

        polygons = self.zone_polygons(source_id, zone_id)
        events: list[dict] = []
        for seg in segs:
            rows = self._provisional_tracklets_for_segment(seg)
            for row in rows:
                if since is not None and row["abs_ts"] < since:
                    continue
                row["id"] = f"p:{row['segment_id']}:{row['class']}:{row['start_off']:.1f}"
                row["provisional"] = True
                row["seg_path"] = seg["path"]
                row["spritesheet"] = seg.get("spritesheet")
                row["seg_media_epoch"] = seg.get("media_epoch")
                _worldize_event_row(row)
                events.append(row)
        events = self.filter_events_with_zone_policy(events, source_id, polygons)
        events.sort(key=lambda r: r["abs_ts"], reverse=True)
        return events

    def provisional_detection_events(
        self,
        source_id: str | None = None,
        since: float | None = None,
        zone_id=None,
    ) -> list[dict]:
        """Live group encounters used specifically by the detection wall."""
        polygons = self.zone_polygons(source_id, zone_id)
        events: list[dict] = []
        for seg in self._provisional_segments(source_id, since):
            for row in self._provisional_encounters_for_segment(seg):
                if since is not None and row["abs_ts"] < since:
                    continue
                row["id"] = (
                    f"g:{row['segment_id']}:{row['class']}:"
                    f"{row['start_off']:.1f}"
                )
                row["provisional"] = True
                row["seg_path"] = seg["path"]
                row["spritesheet"] = seg.get("spritesheet")
                row["seg_media_epoch"] = seg.get("media_epoch")
                _worldize_event_row(row)
                events.append(row)
        events = self.filter_events_with_zone_policy(events, source_id, polygons)
        events.sort(key=lambda row: row["abs_ts"], reverse=True)
        return events

    def _provisional_segments(
        self,
        source_id: str | None,
        since: float | None,
    ) -> list[dict]:
        cutoff = time.time() - _PROVISIONAL_GRACE_SECONDS
        open_cutoff = time.time() - _LIVE_HLS_UNREFERENCED_RETENTION_SECONDS
        epoch = _segment_media_epoch_sql("s")
        coverage_end = f"({epoch} + COALESCE(s.duration_sec, s.end_ts - s.start_ts))"
        where, params = [
            f"{epoch} IS NOT NULL",
            "((s.end_ts IS NULL AND s.start_ts>=?)"
            f" OR (s.end_ts IS NOT NULL AND {coverage_end}>=?"
            " AND s.scanned_at IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM video_events e WHERE e.segment_id=s.id)))"
        ], [open_cutoff, cutoff]
        if source_id and source_id != "all":
            where.append("s.source_id=?"); params.append(source_id)
        if since is not None:
            where.append(f"(s.end_ts IS NULL OR {coverage_end}>=?)")
            params.append(since)
        sql = (
            "SELECT s.* FROM segments s"
            f" WHERE {' AND '.join(where)}"
            f" ORDER BY {epoch} DESC"
        )
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def _provisional_tracklets_for_segment(self, seg: dict) -> list[dict]:
        seg_id = int(seg["id"])
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS watermark"
                " FROM video_detections WHERE segment_id=?",
                (seg_id,),
            ).fetchone()
        watermark = int(row["watermark"]) if row else 0
        with self._provisional_tracklet_cache_lock:
            cached = self._provisional_tracklet_cache.get(seg_id)
            if cached and cached[0] == watermark:
                return [dict(row) for row in cached[1]]

        rows = _object_tracklets_from_detections(
            seg, self.detections_for_segment(seg_id)
        )
        cached_rows = [dict(row) for row in rows]
        with self._provisional_tracklet_cache_lock:
            self._provisional_tracklet_cache[seg_id] = (watermark, cached_rows)
        return [dict(row) for row in cached_rows]

    def _provisional_encounters_for_segment(self, seg: dict) -> list[dict]:
        seg_id = int(seg["id"])
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS watermark"
                " FROM video_detections WHERE segment_id=?",
                (seg_id,),
            ).fetchone()
        watermark = int(row["watermark"]) if row else 0
        with self._provisional_tracklet_cache_lock:
            cached = self._provisional_encounter_cache.get(seg_id)
            if cached and cached[0] == watermark:
                return [dict(row) for row in cached[1]]

        rows = _encounter_events_from_detections(
            seg, self.detections_for_segment(seg_id)
        )
        cached_rows = [dict(row) for row in rows]
        with self._provisional_tracklet_cache_lock:
            self._provisional_encounter_cache[seg_id] = (
                watermark,
                cached_rows,
            )
        return [dict(row) for row in cached_rows]

    def live_status(self, source_id: str | None = None, zone_id=None,
                    det_since: float | None = None,
                    det_until: float | None = None) -> dict:
        with self._connect() as conn:
            where, params = [
                "s.end_ts IS NULL",
                "s.start_ts>=?",
            ], [time.time() - _PROVISIONAL_GRACE_SECONDS]
            if source_id and source_id != "all":
                where.append("s.source_id=?"); params.append(source_id)
            segs = [dict(r) for r in conn.execute(
                "SELECT s.* FROM segments s"
                f" WHERE {' AND '.join(where)}"
                " ORDER BY s.start_ts DESC",
                params,
            ).fetchall()]
            epoch = _segment_media_epoch_sql("s")
            latest_rows = conn.execute(
                "SELECT d.*, s.media_epoch AS media_epoch"
                " FROM video_detections d JOIN segments s ON s.id=d.segment_id"
                f" WHERE {' AND '.join(where)}"
                " AND d.id=("
                "   SELECT d2.id FROM video_detections d2"
                "   WHERE d2.segment_id=d.segment_id"
                "   ORDER BY d2.abs_ts DESC, d2.id DESC LIMIT 1"
                " )"
                " ORDER BY d.abs_ts DESC",
                params,
            ).fetchall()

        # Latest detection from video_detections (backfill, usually absent for live)
        latest: dict[str, dict] = {}
        for r in latest_rows:
            if r["source_id"] in latest:
                continue
            latest[r["source_id"]] = {
                "segment_id": r["segment_id"],
                "source_id": r["source_id"],
                "abs_ts": r["abs_ts"],
                "ts_offset": (float(r["abs_ts"]) - float(r["media_epoch"])
                              if r["media_epoch"] is not None else None),
                "has_human": bool(r["has_human"]),
                "confidence": r["confidence"],
                "boxes": json.loads(r["boxes_json"]) if r["boxes_json"] else [],
                "classes": json.loads(r["classes_json"]) if r["classes_json"] else [],
            }

        # B4: live overlay boxes now come from the live detector's
        # video_detections (subsecond, relay-fed) instead of the retired
        # HLS-tag stream. Each row is already one frame with all its boxes, so
        # no per-class merge is needed. Return ALL recent frames so the client
        # can pick the detection matching the displayed video time (the HLS
        # video player still buffers several seconds behind the live edge).
        if det_since is None:
            if det_until is None:
                det_since = time.time() - 30  # last 30s counts as "live"
            else:
                det_since = float(det_until) - 30.0
        if det_until is None and det_since is not None:
            det_until = float(det_since) + 30.0
        with self._connect() as conn:
            rec_where = ["d.abs_ts >= ?"]
            rec_params: list = [float(det_since)]
            if det_until is not None:
                rec_where.append("d.abs_ts <= ?")
                rec_params.append(float(det_until))
            if source_id and source_id != "all":
                rec_where.append("d.source_id=?"); rec_params.append(source_id)
            rec_rows = conn.execute(
                "SELECT d.source_id, d.abs_ts, d.has_human, d.confidence,"
                " d.boxes_json, d.classes_json"
                f" FROM video_detections d WHERE {' AND '.join(rec_where)}"
                " ORDER BY d.abs_ts",
                rec_params,
            ).fetchall()

        recent = [{
            "source_id": r["source_id"],
            "abs_ts": r["abs_ts"],
            "has_human": bool(r["has_human"]),
            "confidence": r["confidence"],
            "boxes": json.loads(r["boxes_json"]) if r["boxes_json"] else [],
            "classes": json.loads(r["classes_json"]) if r["classes_json"] else [],
        } for r in rec_rows]

        # Latest-per-source for backward compatibility (used by client when no
        # HLS player timing is available)
        for det in recent:
            sid = det["source_id"]
            if sid not in latest or det["abs_ts"] > latest[sid]["abs_ts"]:
                latest[sid] = det

        return {
            "segments": segs,
            "recent_detections": recent,
            "events": self.provisional_events(source_id, zone_id=zone_id),
            "detections": list(latest.values()),
        }


_BACKFILL_DETECTION_FPS = 2.0
# Backward-compatible internal names. The catalog is complete; the actual
# inference whitelist comes from the persisted Settings selection.
_CCTV_CLASSES = COCO_CLASSES
_CCTV_CLASS_IDS = [
    class_id
    for class_id, name in COCO_CLASSES.items()
    if name in DEFAULT_DETECTION_CLASSES
]


def _parse_results(results) -> tuple:
    if not results or results[0].boxes is None or not len(results[0].boxes):
        return False, 0.0, []
    r = results[0]
    boxes = []
    for xyxyn, conf, cls_id in zip(
        r.boxes.xyxyn.tolist(), r.boxes.conf.tolist(), r.boxes.cls.tolist()
    ):
        x1, y1, x2, y2 = xyxyn
        cls = _CCTV_CLASSES.get(int(cls_id), str(int(cls_id)))
        boxes.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2, "conf": conf, "cls": cls})
    has_human = any(b["cls"] == "person" for b in boxes)
    top_conf  = max((b["conf"] for b in boxes if b["cls"] == "person"), default=0.0)
    return has_human, top_conf, boxes


def _yolo_tag_video(
    model,
    seg_path: Path,
    seg_id: int,
    db: VideoSegmentDB,
    predict_lock=None,
    class_ids: list[int] | None = None,
) -> int:
    """Read video file at ~1fps, run YOLO, store detections in clock time.

    Frame time comes from the frame itself: the clock rides as SEI side data,
    read via PyAV (cv2 discards side data). A plausibility clamp on the
    segment's own span keeps any stray SEI CRC-8 false accept from ever writing
    an out-of-sync row.
    """
    import av

    seg_row = db.get_segment(seg_id) or {}
    media_epoch = _seg_media_start(seg_row)
    if media_epoch is None:
        return 0   # unanchored segment: cannot place frames in clock time

    duration = seg_row.get("duration_sec")
    if not isinstance(duration, (int, float)) or duration <= 0:
        duration = _MAX_SEGMENT_SECONDS
    ts_lo = media_epoch - 5.0
    ts_hi = media_epoch + float(duration) + 5.0

    try:
        container = av.open(str(seg_path))
        video_stream = next(
            (s for s in container.streams if s.type == "video"), None)
        if video_stream is None:
            raise ValueError("no video stream")
    except Exception:
        # Sealed MP4 that won't open (corrupt/zero-frame) won't improve on
        # retry — mark it scanned so backfill doesn't re-pick it every loop
        # forever (it kept scanned_at NULL → tight spin). media_epoch-None
        # bails above stay unmarked: those may anchor later.
        LOG.warning("yolo tag cannot open %s — marking scanned", seg_path.name)
        db.mark_scanned(seg_id)
        return 0

    detections: list[dict] = []
    associator = ByteTrackAssociator(
        str(seg_row.get("source_id") or "backfill"),
        _BACKFILL_DETECTION_FPS,
    )
    if class_ids is None:
        class_ids = configured_detection_class_ids(db)
    next_sample_pts: float | None = None
    try:
        for frame in container.decode(video_stream):
            fpts = _frame_pts_seconds(frame)
            if fpts is None:
                continue
            if next_sample_pts is not None and fpts < next_sample_pts:
                continue
            next_sample_pts = (
                fpts + (1.0 / _BACKFILL_DETECTION_FPS)
            )

            abs_ts, crc_ok = sei.decode_frame(frame)
            if not crc_ok or abs_ts is None:
                continue
            # Clamp guards a stray SEI CRC hit to the segment's own span.
            if not (ts_lo <= abs_ts <= ts_hi):
                LOG.warning(
                    "yolo tag rejected implausible clock %.3f at pts %.1fs "
                    "in %s (segment spans %.3f..%.3f)",
                    abs_ts, fpts, seg_path.name, ts_lo, ts_hi,
                )
                continue
            frame_bgr = frame.to_ndarray(format="bgr24")
            try:
                if predict_lock is None:
                    results = model.predict(
                        frame_bgr, classes=class_ids,
                        conf=associator.low_confidence, verbose=False,
                    )
                else:
                    with predict_lock:
                        results = model.predict(
                            frame_bgr, classes=class_ids,
                            conf=associator.low_confidence, verbose=False,
                        )
                _, _, boxes = _parse_results(results)
                result_boxes = (
                    results[0].boxes.cpu().numpy()
                    if results and results[0].boxes is not None
                    else None
                )
                boxes = associator.annotate(
                    result_boxes,
                    boxes,
                    abs_ts=abs_ts,
                )
                has_human = any(
                    box["cls"] == "person" for box in boxes
                )
                conf = max(
                    (
                        float(box["conf"])
                        for box in boxes
                        if box["cls"] == "person"
                    ),
                    default=0.0,
                )
                classes = list({b["cls"] for b in boxes}) if boxes else []
                detections.append({
                    "abs_ts": abs_ts,
                    "has_human": has_human,
                    "confidence": conf,
                    "boxes": boxes,
                    "classes": classes,
                })
            except Exception:
                LOG.exception("yolo tag failed at %.1fs in %s", fpts, seg_path.name)
    except Exception:
        # Decode error mid-file (e.g. truncated tail of a salvaged crash):
        # keep what decoded cleanly, same spirit as the salvage path.
        LOG.warning("yolo tag stopped early in %s", seg_path.name, exc_info=True)
    finally:
        try:
            container.close()
        except Exception:
            pass

    db.replace_detections(seg_id, detections)
    db.mark_scanned(seg_id)
    return len(detections)


def backfill_events(db: VideoSegmentDB, video_dir: Path | None = None,
                    model=None) -> None:
    """YOLO-tag and extract events for closed segments missing detections."""
    with db._connect() as conn:
        segs = conn.execute(
            "SELECT s.* FROM segments s WHERE s.end_ts IS NOT NULL"
            " AND s.media_epoch IS NOT NULL"
            " AND s.scanned_at IS NULL"
            " ORDER BY s.start_ts"
        ).fetchall()
    for row in segs:
        seg = dict(row)
        seg_path = (video_dir / seg["path"]) if video_dir else None
        if model and seg_path and seg_path.exists():
            n_det = _yolo_tag_video(model, seg_path, seg["id"], db)
            LOG.info("backfill tagged %d frames in %s", n_det, seg["path"][-30:])
        # Extract events from detections (new or old)
        dets = db.detections_for_segment(seg["id"])
        n = extract_events(seg, dets, db)
        if n:
            LOG.info("backfill extracted %d events from %s", n, seg["path"][-30:])


def extract_events(segment: dict, detections: list[dict], db: VideoSegmentDB) -> int:
    """Group detections into events and store them."""
    tracklets = _object_tracklets_from_detections(segment, detections)
    object_rows = db.track_object_events(segment, tracklets)
    if object_rows:
        db.insert_object_events(object_rows)
    encounter_rows = _encounter_events_from_detections(segment, detections)
    if encounter_rows:
        db.insert_encounter_events(encounter_rows)
    media_start = _seg_media_start(segment)
    duration = _seg_duration(segment)
    if media_start is not None and duration is not None:
        db.mark_object_derivation(
            segment["source_id"], media_start, media_start + duration
        )
    return len(object_rows)


def rebuild_events(
    db: VideoSegmentDB,
    source_id: str | None = None,
    since: float | None = None,
    until: float | None = None,
    *,
    reset_object_tracks: bool = True,
) -> dict:
    where = ["s.end_ts IS NOT NULL"]
    params: list = []
    if source_id and source_id != "all":
        where.append("s.source_id=?")
        params.append(source_id)
    if since is not None:
        where.append("s.end_ts>=?")
        params.append(since)
    if until is not None:
        where.append("s.start_ts<=?")
        params.append(until)

    with db._connect() as conn:
        segs = [
            dict(r) for r in conn.execute(
                "SELECT s.* FROM segments s"
                f" WHERE {' AND '.join(where)}"
                " ORDER BY s.source_id, s.start_ts",
                params,
            ).fetchall()
        ]
        seg_ids = [s["id"] for s in segs]
        for chunk in _chunks(seg_ids, 500):
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM video_events WHERE segment_id IN ({placeholders})",
                chunk,
            )
            conn.execute(
                f"DELETE FROM object_events WHERE segment_id IN ({placeholders})",
                chunk,
            )
        if reset_object_tracks:
            if source_id and source_id != "all":
                conn.execute("DELETE FROM object_tracks WHERE source_id=?", (source_id,))
                conn.execute("DELETE FROM object_derivations WHERE source_id=?", (source_id,))
            else:
                conn.execute("DELETE FROM object_tracks")
                conn.execute("DELETE FROM object_derivations")

    event_count = 0
    detection_segments = 0
    for seg in segs:
        dets = db.detections_for_segment(seg["id"])
        if dets:
            detection_segments += 1
        event_count += extract_events(seg, dets, db)

    return {
        "segments": len(segs),
        "segments_with_detections": detection_segments,
        "events": event_count,
    }


def derive_object_events(
    db: VideoSegmentDB,
    source_id: str | None = None,
    since: float | None = None,
    until: float | None = None,
    *,
    reset_tracks: bool = True,
) -> dict:
    where = ["s.end_ts IS NOT NULL"]
    params: list = []
    if source_id and source_id != "all":
        where.append("s.source_id=?")
        params.append(source_id)
    if since is not None:
        where.append("s.end_ts>=?")
        params.append(since)
    if until is not None:
        where.append("s.start_ts<=?")
        params.append(until)

    with db._connect() as conn:
        segs = [
            dict(r) for r in conn.execute(
                "SELECT s.* FROM segments s"
                f" WHERE {' AND '.join(where)}"
                " ORDER BY s.source_id, s.start_ts",
                params,
            ).fetchall()
        ]
        sources = sorted({s["source_id"] for s in segs})
        if reset_tracks:
            if source_id and source_id != "all":
                conn.execute("DELETE FROM object_events WHERE source_id=?", (source_id,))
                conn.execute("DELETE FROM object_tracks WHERE source_id=?", (source_id,))
                conn.execute("DELETE FROM object_derivations WHERE source_id=?", (source_id,))
            else:
                conn.execute("DELETE FROM object_events")
                conn.execute("DELETE FROM object_tracks")
                conn.execute("DELETE FROM object_derivations")

    event_count = 0
    detection_segments = 0
    for seg in segs:
        dets = db.detections_for_segment(seg["id"])
        if dets:
            detection_segments += 1
        tracklets = _object_tracklets_from_detections(seg, dets)
        rows = db.track_object_events(seg, tracklets)
        if rows:
            db.insert_object_events(rows)
            event_count += len(rows)

    for source in sources:
        db.mark_object_derivation(source, since, until)

    return {
        "segments": len(segs),
        "segments_with_detections": detection_segments,
        "events": event_count,
        "sources": sources,
    }


def derive_encounter_events(
    db: VideoSegmentDB,
    source_id: str | None = None,
    since: float | None = None,
    until: float | None = None,
) -> dict:
    """Rebuild persisted wall encounters from already-stored detections."""
    where = ["s.end_ts IS NOT NULL"]
    params: list = []
    if source_id and source_id != "all":
        where.append("s.source_id=?")
        params.append(source_id)
    if since is not None:
        where.append("s.end_ts>=?")
        params.append(since)
    if until is not None:
        where.append("s.start_ts<=?")
        params.append(until)

    with db._connect() as conn:
        segments = [
            dict(row) for row in conn.execute(
                "SELECT s.* FROM segments s"
                f" WHERE {' AND '.join(where)}"
                " ORDER BY s.source_id, s.start_ts",
                params,
            ).fetchall()
        ]
        segment_ids = [int(segment["id"]) for segment in segments]
        for chunk in _chunks(segment_ids, 500):
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM video_events WHERE segment_id IN ({placeholders})",
                chunk,
            )

    event_count = 0
    detection_segments = 0
    for segment in segments:
        detections = db.detections_for_segment(segment["id"])
        if detections:
            detection_segments += 1
        rows = _encounter_events_from_detections(segment, detections)
        if rows:
            db.insert_encounter_events(rows)
            event_count += len(rows)
    return {
        "segments": len(segments),
        "segments_with_detections": detection_segments,
        "events": event_count,
    }


def _derivation_covers(row: dict | None, since: float | None, until: float | None) -> bool:
    if not row:
        return False
    marker_since = row.get("since")
    marker_until = row.get("until")
    if marker_since is not None and (since is None or float(marker_since) > since):
        return False
    if marker_until is not None and (until is None or float(marker_until) < until):
        return False
    return True


def _chunks(values: list, size: int):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _object_tracklets_from_detections(segment: dict, detections: list[dict]) -> list[dict]:
    if not detections:
        return []

    media_start = _seg_media_start(segment)
    if media_start is None:
        return []
    tracks: list[dict] = []
    active_indices: list[int] = []

    for det in detections:
        off = float(det.get("ts_offset", 0.0))
        if off < 0:
            continue
        det_abs_ts = det.get("abs_ts")
        if det_abs_ts is None:
            det_abs_ts = media_start + off
        det_abs_ts = float(det_abs_ts)
        boxes = [
            b for b in (det.get("boxes") or [])
            if isinstance(b, dict) and b.get("cls")
        ]
        active_indices = [
            idx for idx in active_indices
            if (
                off - float(tracks[idx]["last"])
                <= (
                    _TRACKED_EVENT_GAP_SECONDS
                    if tracks[idx].get("tracker_id")
                    else _EVENT_GAP_SECONDS
                )
            )
        ]
        used: set[int] = set()
        for box in sorted(boxes, key=lambda b: float(b.get("conf", 0.0)), reverse=True):
            cls = str(box.get("cls"))
            tracker_id = _box_tracker_id(box)
            cx, cy = _box_center(box)
            area = _box_area(box)
            best_idx: int | None = None
            best_dist = _OBJECT_TRACK_CENTER_DISTANCE
            for idx in active_indices:
                track = tracks[idx]
                if idx in used:
                    continue
                if track["class"] != cls:
                    continue
                previous_tracker_id = track.get("tracker_id")
                if tracker_id and previous_tracker_id:
                    if tracker_id == previous_tracker_id:
                        best_idx = idx
                        break
                    continue
                if not _area_compatible(area, track["area"]):
                    continue
                dist = _center_distance(cx, cy, track["cx"], track["cy"])
                if dist <= best_dist:
                    best_idx = idx
                    best_dist = dist

            if best_idx is None:
                tracks.append({
                    "class": cls,
                    "tracker_id": tracker_id,
                    "first": off,
                    "last": off,
                    "first_abs_ts": det_abs_ts,
                    "last_abs_ts": det_abs_ts,
                    "cx": cx,
                    "cy": cy,
                    "area": area,
                    "seen": 1,
                    "confidence": float(box.get("conf", 0.0)),
                    "representative_off": off,
                    "representative_abs_ts": det_abs_ts,
                    "representative_box": dict(box),
                    "first_box": dict(box),
                    "last_box": dict(box),
                })
                new_idx = len(tracks) - 1
                active_indices.append(new_idx)
                used.add(new_idx)
                continue

            track = tracks[best_idx]
            used.add(best_idx)
            track["last"] = off
            track["last_abs_ts"] = det_abs_ts
            track["seen"] += 1
            if tracker_id:
                track["tracker_id"] = tracker_id
                track["representative_box"]["track_id"] = tracker_id
                track["first_box"]["track_id"] = tracker_id
            track["cx"] = (track["cx"] * 0.7) + (cx * 0.3)
            track["cy"] = (track["cy"] * 0.7) + (cy * 0.3)
            track["area"] = (track["area"] * 0.7) + (area * 0.3)
            track["last_box"] = dict(box)
            conf = float(box.get("conf", 0.0))
            if conf > track["confidence"]:
                track["confidence"] = conf
                track["representative_off"] = off
                track["representative_abs_ts"] = det_abs_ts
                track["representative_box"] = dict(box)

    rows: list[dict] = []
    for track in tracks:
        if track["seen"] < _OBJECT_MIN_OBSERVATIONS:
            continue
        representative_box = dict(track["representative_box"])
        representative_box["cls"] = track["class"]
        first_box = dict(track["first_box"])
        first_box["cls"] = track["class"]
        last_box = dict(track["last_box"])
        last_box["cls"] = track["class"]
        rows.append({
            "segment_id": segment["id"],
            "source_id": segment["source_id"],
            "abs_ts": float(track["representative_abs_ts"]),
            "display_ts": float(track["representative_abs_ts"]),
            "class": track["class"],
            "start_off": float(track["representative_off"]),
            "end_off": float(track["last"]),
            "confidence": float(track["confidence"]),
            "observations": int(track["seen"]),
            "boxes_json": json.dumps([representative_box]),
            "track_first_abs_ts": float(track["first_abs_ts"]),
            "track_first_off": float(track["first"]),
            "first_boxes_json": json.dumps([first_box]),
            "track_last_abs_ts": float(track["last_abs_ts"]),
            "last_boxes_json": json.dumps([last_box]),
        })
    rows.sort(key=lambda r: (r["abs_ts"], r["class"]))
    return rows


def _encounter_events_from_detections(
    segment: dict,
    detections: list[dict],
) -> list[dict]:
    """Collapse nearby same-class objects into spatial-temporal encounters."""
    if not detections:
        return []
    media_start = _seg_media_start(segment)
    if media_start is None:
        return []

    tracks: list[dict] = []
    active_indices: list[int] = []
    for detection in detections:
        off = float(detection.get("ts_offset", 0.0))
        if off < 0:
            continue
        abs_ts = float(detection.get("abs_ts", media_start + off))
        groups = _encounter_frame_groups([
            box for box in (detection.get("boxes") or [])
            if isinstance(box, dict) and box.get("cls")
        ])
        active_indices = [
            index for index in active_indices
            if abs_ts - float(tracks[index]["last_abs_ts"])
            <= _ENCOUNTER_GAP_SECONDS
        ]
        used_tracks: set[int] = set()
        for group in sorted(
            groups,
            key=lambda item: float(item["confidence"]),
            reverse=True,
        ):
            cx, cy = _box_center(group["union"])
            area = _box_area(group["union"])
            best_index: int | None = None
            best_score = float("inf")
            for index in active_indices:
                if index in used_tracks:
                    continue
                track = tracks[index]
                if track["class"] != group["class"]:
                    continue
                gap = abs_ts - float(track["last_abs_ts"])
                if gap < 0 or gap > _ENCOUNTER_GAP_SECONDS:
                    continue
                ratio = _area_ratio_values(area, float(track["area"]))
                if ratio is None or ratio > _ENCOUNTER_MAX_AREA_RATIO:
                    continue
                predicted_x = float(track["cx"]) + float(track["vx"]) * gap
                predicted_y = float(track["cy"]) + float(track["vy"]) * gap
                distance = _center_distance(cx, cy, predicted_x, predicted_y)
                max_distance = min(
                    _ENCOUNTER_MAX_PREDICTED_DISTANCE,
                    _ENCOUNTER_MOTION_BASE_DISTANCE
                    + _ENCOUNTER_MOTION_SPEED_DISTANCE * gap,
                )
                if distance > max_distance:
                    continue
                shared_tokens = (
                    set(track["tracker_ids"]) & set(group["tracker_ids"])
                )
                score = (
                    distance
                    + abs(math.log(ratio)) * 0.015
                    - (0.04 if shared_tokens else 0.0)
                )
                if score < best_score:
                    best_score = score
                    best_index = index

            if best_index is None:
                tracks.append({
                    "class": group["class"],
                    "first": off,
                    "last": off,
                    "first_abs_ts": abs_ts,
                    "last_abs_ts": abs_ts,
                    "cx": cx,
                    "cy": cy,
                    "vx": 0.0,
                    "vy": 0.0,
                    "area": area,
                    "seen": 1,
                    "confidence": float(group["confidence"]),
                    "representative_boxes": group["boxes"],
                    "area_boxes": [],
                    "area_cells": {
                        cell for cell in (
                            _encounter_area_cell(box)
                            for box in group["boxes"]
                        )
                        if cell is not None
                    },
                    "first_box": dict(group["union"]),
                    "last_box": dict(group["union"]),
                    "tracker_ids": set(group["tracker_ids"]),
                })
                index = len(tracks) - 1
                active_indices.append(index)
                used_tracks.add(index)
                continue

            track = tracks[best_index]
            used_tracks.add(best_index)
            elapsed = abs_ts - float(track["last_abs_ts"])
            if elapsed > 0:
                measured_vx = (cx - float(track["cx"])) / elapsed
                measured_vy = (cy - float(track["cy"])) / elapsed
                track["vx"] = float(track["vx"]) * 0.4 + measured_vx * 0.6
                track["vy"] = float(track["vy"]) * 0.4 + measured_vy * 0.6
            track["last"] = off
            track["last_abs_ts"] = abs_ts
            track["cx"] = cx
            track["cy"] = cy
            track["area"] = area
            track["seen"] += 1
            track["last_box"] = dict(group["union"])
            track["tracker_ids"].update(group["tracker_ids"])
            _append_encounter_area_boxes(track, group["boxes"])
            track["confidence"] = max(
                float(track["confidence"]),
                float(group["confidence"]),
            )

    rows: list[dict] = []
    for track in tracks:
        if int(track["seen"]) < _OBJECT_MIN_OBSERVATIONS:
            continue
        state = {
            "kind": "encounter-v1",
            "last_abs_ts": float(track["last_abs_ts"]),
            "last_box": track["last_box"],
            "vx": float(track["vx"]),
            "vy": float(track["vy"]),
            "tracker_ids": sorted(track["tracker_ids"]),
        }
        rows.append({
            "segment_id": segment["id"],
            "source_id": segment["source_id"],
            "abs_ts": float(track["first_abs_ts"]),
            "class": track["class"],
            "start_off": float(track["first"]),
            "end_off": float(track["last"]),
            "confidence": float(track["confidence"]),
            "boxes_json": json.dumps(
                [
                    *track["representative_boxes"],
                    *track["area_boxes"],
                ],
                separators=(",", ":"),
            ),
            "event_type": "detection",
            "track_id": json.dumps(state, separators=(",", ":")),
            "_encounter_first_abs_ts": float(track["first_abs_ts"]),
            "_encounter_first_box": track["first_box"],
            "_encounter_tracker_ids": sorted(track["tracker_ids"]),
        })
    rows.sort(key=lambda row: (row["abs_ts"], row["class"]))
    return rows


def _encounter_frame_groups(boxes: list[dict]) -> list[dict]:
    components: list[list[dict]] = []
    for box in sorted(
        boxes,
        key=lambda item: float(item.get("conf", 0.0)),
        reverse=True,
    ):
        matching = [
            index for index, component in enumerate(components)
            if component[0].get("cls") == box.get("cls")
            and any(_encounter_boxes_near(box, other) for other in component)
        ]
        if not matching:
            components.append([dict(box)])
            continue
        target = matching[0]
        components[target].append(dict(box))
        for index in reversed(matching[1:]):
            components[target].extend(components.pop(index))

    groups = []
    for component in components:
        cls = str(component[0]["cls"])
        confidence = max(float(box.get("conf", 0.0)) for box in component)
        union = {
            "cls": cls,
            "conf": confidence,
            "x1": min(float(box["x1"]) for box in component),
            "y1": min(float(box["y1"]) for box in component),
            "x2": max(float(box["x2"]) for box in component),
            "y2": max(float(box["y2"]) for box in component),
            "group_size": len(component),
        }
        tracker_ids = {
            tracker_id for tracker_id in (
                _box_tracker_id(box) for box in component
            )
            if tracker_id
        }
        groups.append({
            "class": cls,
            "confidence": confidence,
            "union": union,
            "boxes": (
                [union]
                if len(component) == 1
                else [union, *component]
            ),
            "tracker_ids": tracker_ids,
        })
    return groups


def _encounter_area_cell(box: dict) -> tuple[int, int] | None:
    try:
        cx, cy = _box_center(box)
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(cx) or not math.isfinite(cy):
        return None
    return (
        max(0, min(99, int(cx * 100))),
        max(0, min(99, int(cy * 100))),
    )


def _append_encounter_area_boxes(track: dict, boxes: list[dict]) -> None:
    """Retain one box per visited 1%-of-frame cell for area filtering."""
    cells = track["area_cells"]
    for box in boxes:
        cell = _encounter_area_cell(box)
        if cell is None or cell in cells:
            continue
        sample = {
            key: box[key]
            for key in ("cls", "x1", "y1", "x2", "y2")
            if key in box
        }
        sample["_zone_sample"] = True
        track["area_boxes"].append(sample)
        cells.add(cell)


def _merge_encounter_area_boxes(existing_raw, incoming_raw) -> str:
    try:
        existing = json.loads(existing_raw) if existing_raw else []
    except (TypeError, json.JSONDecodeError):
        existing = []
    try:
        incoming = json.loads(incoming_raw) if incoming_raw else []
    except (TypeError, json.JSONDecodeError):
        incoming = []
    if not isinstance(existing, list):
        existing = []
    if not isinstance(incoming, list):
        incoming = []
    cells = {
        cell for cell in (
            _encounter_area_cell(box)
            for box in existing
            if isinstance(box, dict)
        )
        if cell is not None
    }
    for box in incoming:
        if not isinstance(box, dict):
            continue
        cell = _encounter_area_cell(box)
        if cell is None or cell in cells:
            continue
        sample = dict(box)
        sample["_zone_sample"] = True
        existing.append(sample)
        cells.add(cell)
    return json.dumps(existing, separators=(",", ":"))


def _encounter_boxes_near(first: dict, second: dict) -> bool:
    try:
        first_cx, first_cy = _box_center(first)
        second_cx, second_cy = _box_center(second)
        first_size = max(
            float(first["x2"]) - float(first["x1"]),
            float(first["y2"]) - float(first["y1"]),
        )
        second_size = max(
            float(second["x2"]) - float(second["x1"]),
            float(second["y2"]) - float(second["y1"]),
        )
    except (KeyError, TypeError, ValueError):
        return False
    return _center_distance(
        first_cx, first_cy, second_cx, second_cy
    ) <= max(
        _ENCOUNTER_GROUP_BASE_DISTANCE,
        _ENCOUNTER_GROUP_SIZE_FACTOR * max(first_size, second_size),
    )


def _area_ratio_values(first: float, second: float) -> float | None:
    smaller, larger = sorted((float(first), float(second)))
    if smaller <= 0:
        return None
    return larger / smaller


def _encounter_state(raw) -> dict | None:
    if not raw:
        return None
    try:
        state = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict) or state.get("kind") != "encounter-v1":
        return None
    return state


def _public_object_event(event: dict) -> dict:
    row = dict(event)
    if row.get("display_ts") is not None:
        row["abs_ts"] = row["display_ts"]
    row["id"] = f"o:{row['id']}"
    return row


def _event_boxes(event: dict) -> list[dict]:
    try:
        boxes = json.loads(event["boxes_json"]) if event.get("boxes_json") else []
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(boxes, list):
        return []
    return [box for box in boxes if isinstance(box, dict)]


def _event_box(event: dict) -> dict | None:
    return next(iter(_event_boxes(event)), None)


def _box_tracker_id(box: dict | None) -> str | None:
    if not isinstance(box, dict):
        return None
    value = box.get("track_id")
    if value in (None, ""):
        return None
    return str(value)


def _event_tracker_id(event: dict) -> str | None:
    return _box_tracker_id(_event_box(event))


def _object_event_thumbnail_abs_ts(
    conn: sqlite3.Connection,
    event: dict,
) -> float:
    """Resolve the frame that actually supplied an object event's crop box.

    Object events written before synchronized representative observations
    could combine the track's first timestamp with its highest-confidence box
    from a later frame.  The external tracker token survives in both the event
    box and raw detections, so old rows can be repaired at read time without a
    destructive backfill.
    """
    event_ts = float(event["abs_ts"])
    event_box = _event_box(event)
    tracker_id = _box_tracker_id(event_box)
    segment_id = event.get("segment_id")
    if not event_box or not tracker_id or segment_id is None:
        return event_ts

    best_ts = event_ts
    best_score: tuple[float, float, float] | None = None

    def consider(rows) -> None:
        nonlocal best_score, best_ts
        for row in rows:
            try:
                boxes = json.loads(row["boxes_json"]) if row["boxes_json"] else []
            except (TypeError, json.JSONDecodeError):
                continue
            for candidate in boxes:
                if not isinstance(candidate, dict):
                    continue
                if _box_tracker_id(candidate) != tracker_id:
                    continue
                if candidate.get("cls") != event_box.get("cls"):
                    continue
                try:
                    coordinate_delta = sum(
                        abs(float(candidate[key]) - float(event_box[key]))
                        for key in ("x1", "y1", "x2", "y2")
                    )
                    confidence_delta = abs(
                        float(candidate.get("conf", 0.0))
                        - float(event_box.get("conf", 0.0))
                    )
                    candidate_ts = float(row["abs_ts"])
                except (KeyError, TypeError, ValueError):
                    continue
                score = (
                    coordinate_delta,
                    confidence_delta,
                    abs(candidate_ts - event_ts),
                )
                if best_score is None or score < best_score:
                    best_score = score
                    best_ts = candidate_ts

    # Events written by the synchronized pipeline already point at the right
    # raw observation. Keep their common thumbnail lookup to a single row.
    nearby = conn.execute(
        "SELECT abs_ts, boxes_json FROM video_detections"
        " WHERE segment_id=? AND abs_ts BETWEEN ? AND ?",
        (segment_id, event_ts - 0.001, event_ts + 0.001),
    ).fetchall()
    consider(nearby)
    if (
        best_score is not None
        and best_score[0] < 1e-9
        and best_score[1] < 1e-9
    ):
        return best_ts

    escaped_tracker_id = (
        tracker_id.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    rows = conn.execute(
        "SELECT abs_ts, boxes_json FROM video_detections"
        " WHERE segment_id=? AND boxes_json LIKE ? ESCAPE '\\'"
        " ORDER BY abs_ts",
        (segment_id, f"%{escaped_tracker_id}%"),
    ).fetchall()
    consider(rows)
    return best_ts


def _box_center(box: dict) -> tuple[float, float]:
    return (
        (float(box["x1"]) + float(box["x2"])) / 2,
        (float(box["y1"]) + float(box["y2"])) / 2,
    )


def _box_area(box: dict) -> float:
    return max(0.0, float(box["x2"]) - float(box["x1"])) * max(
        0.0, float(box["y2"]) - float(box["y1"])
    )


def _center_distance(ax: float, ay: float, bx: float, by: float) -> float:
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def _area_compatible(area: float, other: float) -> bool:
    if area <= 0 or other <= 0:
        return False
    ratio = max(area, other) / min(area, other)
    return ratio <= _OBJECT_TRACK_AREA_RATIO


def _sanitize_zone(source_id: str, zone: dict) -> dict:
    if not isinstance(zone, dict):
        raise ValueError("zone must be an object")
    zone_type = str(zone.get("type") or zone.get("zone_type") or "activity_area").strip()
    if zone_type not in {"activity_area", "vehicle_event", "exclusion_area"}:
        raise ValueError("unsupported zone type")
    polygon = _normalize_polygon(zone.get("polygon"))
    if zone_type == "vehicle_event":
        zone_type = "activity_area"
    default_name = "Exclusion area" if zone_type == "exclusion_area" else "Activity area"
    name = str(zone.get("name") or default_name).strip()[:80] or default_name
    return {
        "source_id": source_id,
        "name": name,
        "zone_type": zone_type,
        "polygon_json": json.dumps(polygon, separators=(",", ":")),
        "enabled": 1 if zone.get("enabled", True) else 0,
    }


def _new_zone_uid() -> str:
    return uuid.uuid4().hex


def _normalize_zone_uid(raw) -> str | None:
    uid = str(raw or "").strip()
    if not uid or len(uid) > 80:
        return None
    if any(not (ch.isalnum() or ch in {"-", "_"}) for ch in uid):
        return None
    return uid


def _notification_rule_from_row(row: sqlite3.Row | None) -> dict:
    if row is None:
        raise ValueError("notification rule row is required")
    try:
        classes = json.loads(row["classes_json"]) if row["classes_json"] else []
    except (TypeError, json.JSONDecodeError):
        classes = []
    if not isinstance(classes, list):
        classes = []
    return {
        "id": row["id"],
        "name": row["name"],
        "source_id": row["source_id"],
        "zone_ref": row["zone_ref"],
        "classes": [str(c) for c in classes if str(c).strip()],
        "enabled": bool(row["enabled"]),
        "cooldown_seconds": int(row["cooldown_seconds"] or 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _sanitize_notification_rule(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("rule must be an object")
    source_id = str(data.get("source_id") or "").strip()
    if not source_id or source_id == "all":
        raise ValueError("source_id is required")
    name = str(data.get("name") or "Notification rule").strip()[:120]
    if not name:
        name = "Notification rule"

    raw_ref = str(data.get("zone_ref") or data.get("zone") or "whole_frame").strip()
    aliases = {
        "frame": "whole_frame",
        "whole-frame": "whole_frame",
        "whole_frame": "whole_frame",
        "none": "whole_frame",
        "off": "whole_frame",
        "all": "all_activity_areas",
        "all_zones": "all_activity_areas",
        "all_activity_areas": "all_activity_areas",
    }
    if raw_ref.startswith("zone:"):
        uid = _normalize_zone_uid(raw_ref.split(":", 1)[1])
        if not uid:
            raise ValueError("zone_ref is invalid")
        zone_ref = f"zone:{uid}"
    else:
        zone_ref = aliases.get(raw_ref)
        if not zone_ref:
            raise ValueError("zone_ref is invalid")

    try:
        cooldown = int(float(data.get("cooldown_seconds", 60)))
    except (TypeError, ValueError):
        cooldown = 60
    cooldown = max(0, min(86400, cooldown))

    enabled_raw = data.get("enabled", True)
    if isinstance(enabled_raw, str):
        enabled = enabled_raw.strip().lower() not in {"0", "false", "no", "off", "paused"}
    else:
        enabled = bool(enabled_raw)

    return {
        "name": name,
        "source_id": source_id,
        "zone_ref": zone_ref,
        "classes_json": json.dumps(
            _sanitize_notification_classes(data), separators=(",", ":")
        ),
        "enabled": 1 if enabled else 0,
        "cooldown_seconds": cooldown,
    }


def _sanitize_notification_classes(data: dict) -> list[str]:
    raw = data.get("classes")
    if raw is None and data.get("classes_json"):
        try:
            raw = json.loads(data["classes_json"])
        except (TypeError, json.JSONDecodeError):
            raw = []
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, list):
        items = raw
    else:
        items = []

    classes: list[str] = []
    seen: set[str] = set()
    for item in items:
        cls = str(item).strip().lower()[:64]
        if not cls or cls in seen:
            continue
        seen.add(cls)
        classes.append(cls)
    return classes


def _validate_notification_zone_ref(
    _conn: sqlite3.Connection, _source_id: str, zone_ref: str
) -> None:
    if zone_ref.startswith("zone:") and not _normalize_zone_uid(zone_ref.split(":", 1)[1]):
        raise ValueError("zone_ref is invalid")


def _notification_event_from_row(row: sqlite3.Row | None) -> dict:
    if row is None:
        raise ValueError("notification event row is required")
    try:
        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    thumb_url = row["thumb_url"]
    if "thumb_jpeg" in row.keys() and row["thumb_jpeg"]:
        thumb_url = f"/api/notifications/{row['id']}/thumb"
    elif metadata.get("event_kind") == "hls":
        thumb_url = None
    return {
        "id": row["id"],
        "rule_id": row["rule_id"],
        "rule_name": row["rule_name"],
        "source_id": row["source_id"],
        "zone_ref": row["zone_ref"],
        "event_ref": row["event_ref"],
        "event_ts": row["event_ts"],
        "class": row["class"],
        "confidence": row["confidence"],
        "title": row["title"],
        "body": row["body"],
        "thumb_url": thumb_url,
        "target_url": row["target_url"],
        "read_at": row["read_at"],
        "dismissed_at": row["dismissed_at"],
        "created_at": row["created_at"],
        "read": row["read_at"] is not None,
        "metadata": metadata,
    }


def _notification_confirmation_enabled() -> bool:
    raw = os.environ.get("NOTIFICATION_CONFIRM_YOLO", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _notification_confirmation_from_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    try:
        boxes = json.loads(row["boxes_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        boxes = []
    return {
        "status": row["status"],
        "confirmed": bool(row["confirmed"]),
        "confidence": float(row["confidence"] or 0.0),
        "reason": row["reason"] or "",
        "strategy_version": row["strategy_version"],
        "metadata": metadata if isinstance(metadata, dict) else {},
        "boxes": boxes if isinstance(boxes, list) else [],
        "updated_at": float(row["updated_at"] or 0.0),
    }


def _confirm_notification_with_yolo(event: dict, event_ref: str) -> dict:
    req = {
        "type": "confirm_notification_event",
        "strategy_version": _NOTIFICATION_CONFIRMATION_STRATEGY,
        "event_ref": event_ref,
        "source_id": str(event.get("source_id") or ""),
        "abs_ts": float(event.get("abs_ts") or 0.0),
        "class": str(event.get("class") or ""),
        "boxes_json": event.get("boxes_json") or "[]",
        "event_kind": event.get("_kind", "event"),
    }
    try:
        resp = _yolo_socket_request(req, _NOTIFICATION_CONFIRMATION_TIMEOUT_SECONDS)
    except Exception as exc:
        return {
            "status": "error",
            "confirmed": False,
            "confidence": 0.0,
            "reason": f"yolo_socket_error:{type(exc).__name__}",
            "strategy_version": _NOTIFICATION_CONFIRMATION_STRATEGY,
            "metadata": {"error": str(exc)[:200]},
            "boxes": [],
        }
    if resp.get("status") != "ok":
        return {
            "status": "error",
            "confirmed": False,
            "confidence": 0.0,
            "reason": str(resp.get("error") or "yolo_error")[:160],
            "strategy_version": _NOTIFICATION_CONFIRMATION_STRATEGY,
            "metadata": {"response": resp},
            "boxes": [],
        }
    confirmed = bool(resp.get("confirmed"))
    reason = str(resp.get("reason") or "")
    if not confirmed and reason == "frame_unavailable":
        return {
            "status": "error",
            "confirmed": False,
            "confidence": 0.0,
            "reason": reason,
            "strategy_version": str(
                resp.get("strategy_version") or _NOTIFICATION_CONFIRMATION_STRATEGY
            ),
            "metadata": _compact_confirmation_metadata(resp),
            "boxes": [],
        }
    return {
        "status": "confirmed" if confirmed else "rejected",
        "confirmed": confirmed,
        "confidence": float(resp.get("confidence") or 0.0),
        "reason": reason,
        "strategy_version": str(
            resp.get("strategy_version") or _NOTIFICATION_CONFIRMATION_STRATEGY
        ),
        "metadata": _compact_confirmation_metadata(resp),
        "boxes": [resp["box"]] if isinstance(resp.get("box"), dict) else [],
    }


def _compact_confirmation_metadata(resp: dict) -> dict:
    metadata: dict = {
        "strategy": str(resp.get("strategy_version") or _NOTIFICATION_CONFIRMATION_STRATEGY),
        "reason": str(resp.get("reason") or ""),
        "frame_source": resp.get("frame_source"),
        "full_confidence": _safe_round(resp.get("full_confidence")),
        "crop_confidence": _safe_round(resp.get("crop_confidence")),
    }
    timings = resp.get("timings_ms")
    if isinstance(timings, dict):
        metadata["timings_ms"] = {
            str(k): _safe_round(v, digits=1) for k, v in timings.items()
        }
    return {k: v for k, v in metadata.items() if v is not None and v != ""}


def _safe_round(value, *, digits: int = 3):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _url_ts(value) -> str:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        ts = 0.0
    return f"{ts:.3f}".rstrip("0").rstrip(".")


def _yolo_socket_request(req: dict, timeout: float) -> dict:
    sock_path = os.environ.get("YOLO_SOCKET", "/tmp/yolo.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(max(0.1, float(timeout)))
        s.connect(sock_path)
        s.sendall((json.dumps(req, separators=(",", ":")) + "\n").encode())
        chunks: list[bytes] = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise RuntimeError("empty yolo response")
    return json.loads(raw.decode())


# Detection sources a rule draws candidates from. Each is a table read in
# insertion-id order; the cursor (notification_cursor) holds the highest id
# already considered. Insertion-id ordering — not abs_ts — means late,
# backdated rows are still picked up (their id is always above the cursor).
#   hls = real-time live feed (small, pruned at 2h)
#   det = per-frame detections from recorded segments. Unlike object_events
#         'appeared', this sees the whole presence, not just the entry instant,
#         so a person who walks into a zone is caught.
# The live detector's video_detections ('det') is the authoritative live pass.
# (The retired 'hls' HLS-tag-loop path and its hls_events store are gone.)
_NOTIFICATION_KINDS = ("det",)
_NOTIFICATION_KIND_MAX_ID_TABLE = {"det": "video_detections"}


def _notification_kind_max_id(conn: sqlite3.Connection, kind: str) -> int:
    table = _NOTIFICATION_KIND_MAX_ID_TABLE.get(kind)
    if not table:
        return 0
    row = conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def _notification_cursor_get(conn: sqlite3.Connection, rule_id: int, kind: str) -> int:
    row = conn.execute(
        "SELECT last_id FROM notification_cursor WHERE rule_id=? AND kind=?",
        (rule_id, kind),
    ).fetchone()
    if row is not None:
        return int(row[0])
    # First encounter: seed forward-only at the current max id so a freshly
    # created rule (or first ever run) never replays the retained backlog.
    seed = _notification_kind_max_id(conn, kind)
    conn.execute(
        "INSERT OR IGNORE INTO notification_cursor(rule_id, kind, last_id) VALUES(?,?,?)",
        (rule_id, kind, seed),
    )
    return seed


def _notification_cursor_set(
    conn: sqlite3.Connection, rule_id: int, kind: str, last_id: int
) -> None:
    conn.execute(
        "INSERT INTO notification_cursor(rule_id, kind, last_id) VALUES(?,?,?)"
        " ON CONFLICT(rule_id, kind) DO UPDATE SET last_id=excluded.last_id"
        " WHERE excluded.last_id > notification_cursor.last_id",
        (rule_id, kind, last_id),
    )


def _notification_events_after(
    conn: sqlite3.Connection,
    rule: dict,
    kind: str,
    start_id: int,
    limit: int,
) -> tuple[list[dict], int]:
    """Fetch a rule's candidate events of one kind with id > start_id.

    Returns (filtered events, max raw id seen). max_id covers rows filtered out
    too, so the cursor advances past them and they are never re-examined.

    Both fetchers scan by primary-key rowid range (id > start_id) and filter
    source in Python rather than in SQL. Leading the SQL with source_id makes
    the planner ignore the id range and table-scan every row of the source
    (temp b-tree sort), so a tick would re-read the whole table; the rowid
    range reads only rows newer than the cursor. The range is contiguous, so no
    row of the wanted source is skipped by filtering afterwards.
    """
    polygons = _notification_zone_polygons(conn, rule["source_id"], rule["zone_ref"])
    if polygons is None:
        return [], start_id
    exclusions = _notification_exclusion_polygons(conn, rule["source_id"])
    if kind == "det":
        return _notification_det_events_after(
            conn, rule, polygons, exclusions, start_id, limit
        )
    return [], start_id


def _notification_det_events_after(
    conn: sqlite3.Connection,
    rule: dict,
    polygons: list[list[dict]],
    exclusions: list[list[dict]],
    start_id: int,
    limit: int,
) -> tuple[list[dict], int]:
    """Per-frame detections (video_detections) as notification candidates.

    Surfaces every tagged frame, keyed by the stored universal abs_ts. Media
    offsets remain private metadata for frame extraction.
    """
    source_id = rule["source_id"]
    classes = set(rule.get("classes") or [])
    rows = conn.execute(
        "SELECT vd.id AS id, vd.source_id AS source_id, vd.abs_ts AS abs_ts,"
        " vd.confidence AS confidence, vd.boxes_json AS boxes_json"
        " FROM video_detections vd"
        " WHERE vd.id>? ORDER BY vd.id ASC LIMIT ?",
        (start_id, max(1, int(limit))),
    ).fetchall()

    max_id = start_id
    events: list[dict] = []
    for r in rows:
        row = dict(r)
        if row["id"] > max_id:
            max_id = row["id"]
        if row["source_id"] != source_id:
            continue
        try:
            boxes = json.loads(row["boxes_json"]) if row["boxes_json"] else []
        except (TypeError, json.JSONDecodeError):
            boxes = []
        # Highest-confidence box of a wanted class whose centre sits in the zone.
        best = None
        for box in boxes:
            if not isinstance(box, dict):
                continue
            cls = box.get("cls")
            if not cls or (classes and cls not in classes):
                continue
            try:
                cx = (float(box["x1"]) + float(box["x2"])) / 2
                cy = (float(box["y1"]) + float(box["y2"])) / 2
            except (KeyError, TypeError, ValueError):
                continue
            if polygons and not _point_in_any_polygon(cx, cy, polygons):
                continue
            if exclusions and _point_in_any_polygon(cx, cy, exclusions):
                continue
            if best is None or float(box.get("conf") or 0) > float(best.get("conf") or 0):
                best = box
        if best is None:
            continue
        events.append({
            "id": row["id"],
            "source_id": row["source_id"],
            "abs_ts": float(row["abs_ts"]),
            "class": best.get("cls"),
            "confidence": float(best.get("conf") or 0),
            "boxes_json": json.dumps([best], separators=(",", ":")),
            "_kind": "det",
        })
    return events, max_id


def _notification_zone_polygons(
    conn: sqlite3.Connection,
    source_id: str,
    zone_ref: str,
) -> list[list[dict]] | None:
    if zone_ref == "whole_frame":
        return []
    if zone_ref == "all_activity_areas":
        zones = _notification_activity_zones(conn, source_id)
        return [z["polygon"] for z in zones] if zones else None
    if zone_ref.startswith("zone:"):
        uid = zone_ref.split(":", 1)[1]
        zone = _notification_zone_by_uid(conn, source_id, uid)
        if not zone:
            return None
        return [zone["polygon"]]
    return None


def _notification_activity_zones(
    conn: sqlite3.Connection,
    source_id: str,
) -> list[dict]:
    rows = conn.execute(
        "SELECT id, uid, name, polygon_json FROM video_zones"
        " WHERE source_id=? AND zone_type='activity_area' AND enabled=1"
        " ORDER BY id",
        (source_id,),
    ).fetchall()
    zones = []
    for row in rows:
        try:
            polygon = json.loads(row["polygon_json"])
        except (TypeError, json.JSONDecodeError):
            polygon = []
        if isinstance(polygon, list) and len(polygon) >= 3:
            zones.append({
                "id": row["id"],
                "uid": row["uid"],
                "name": row["name"],
                "polygon": polygon,
            })
    return zones


def _notification_exclusion_polygons(
    conn: sqlite3.Connection,
    source_id: str,
) -> list[list[dict]]:
    rows = conn.execute(
        "SELECT polygon_json FROM video_zones"
        " WHERE source_id=? AND zone_type='exclusion_area' AND enabled=1"
        " ORDER BY id",
        (source_id,),
    ).fetchall()
    polygons: list[list[dict]] = []
    for row in rows:
        try:
            polygon = json.loads(row["polygon_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(polygon, list) and len(polygon) >= 3:
            polygons.append(polygon)
    return polygons


def _notification_zone_by_uid(
    conn: sqlite3.Connection,
    source_id: str,
    uid: str,
) -> dict | None:
    row = conn.execute(
        "SELECT id, uid, name, polygon_json FROM video_zones"
        " WHERE source_id=? AND uid=? AND zone_type='activity_area'"
        " AND enabled=1 LIMIT 1",
        (source_id, uid),
    ).fetchone()
    if not row:
        return None
    try:
        polygon = json.loads(row["polygon_json"])
    except (TypeError, json.JSONDecodeError):
        polygon = []
    if not isinstance(polygon, list) or len(polygon) < 3:
        return None
    return {
        "id": row["id"],
        "uid": row["uid"],
        "name": row["name"],
        "polygon": polygon,
    }


def _notification_zone_label_and_param(
    conn: sqlite3.Connection,
    source_id: str,
    zone_ref: str,
) -> tuple[str, str]:
    if zone_ref == "whole_frame":
        return "Whole frame", "none"
    if zone_ref == "all_activity_areas":
        return "All activity areas", "all"
    if zone_ref.startswith("zone:"):
        zone = _notification_zone_by_uid(conn, source_id, zone_ref.split(":", 1)[1])
        if zone:
            return zone["name"] or f"Area {zone['id']}", str(zone["id"])
        return "Missing area", "none"
    return zone_ref, "none"


def _build_notification_event(
    conn: sqlite3.Connection,
    rule: dict,
    event: dict,
    created_at: float,
) -> dict:
    zone_label, zone_param = _notification_zone_label_and_param(
        conn, rule["source_id"], rule["zone_ref"]
    )
    cls = str(event["class"])
    pretty_cls = cls.replace("_", " ").strip().title() or "Object"
    event_ref = _notification_event_ref(event)
    params = {
        "source": rule["source_id"],
        "ts": _url_ts(event["abs_ts"]),
        "cls": cls,
        "zone": zone_param,
    }
    thumb_url = _notification_thumb_url(event, event_ref)
    metadata = {
        "zone_label": zone_label,
        "zone_param": zone_param,
        "event_kind": event.get("_kind", "event"),
    }
    confirmation = event.get("_confirmation")
    if isinstance(confirmation, dict):
        metadata["confirmation"] = {
            "status": confirmation.get("status"),
            "strategy": confirmation.get("strategy_version"),
            "confidence": _safe_round(confirmation.get("confidence")),
            "reason": confirmation.get("reason"),
            **(confirmation.get("metadata") or {}),
        }
    return {
        "rule_id": rule["id"],
        "rule_name": rule["name"],
        "source_id": rule["source_id"],
        "zone_ref": rule["zone_ref"],
        "event_ref": event_ref,
        "event_ts": float(event["abs_ts"]),
        "class": cls,
        "confidence": float(event.get("confidence") or 0),
        "title": f"{pretty_cls} detected",
        "body": f"{rule['name']} · {zone_label}",
        "thumb_url": thumb_url,
        "thumb_jpeg": event.get("thumb_jpeg"),
        "target_url": f"/?{urlencode(params)}",
        "created_at": created_at,
        "metadata_json": json.dumps(metadata, separators=(",", ":")),
    }


def _notification_event_ref(event: dict) -> str:
    kind = event.get("_kind")
    if kind == "object":
        return f"o:{event['id']}"
    if kind == "det":
        return f"d:{event['id']}"
    return str(event["id"])


def _notification_thumb_url(event: dict, event_ref: str) -> str:
    return f"/api/video/event-thumb/{event_ref}"


def _normalize_polygon(raw) -> list[dict]:
    if not isinstance(raw, list) or len(raw) < 3:
        raise ValueError("polygon must contain at least three points")
    points: list[dict] = []
    for point in raw:
        if isinstance(point, dict):
            x = float(point.get("x"))
            y = float(point.get("y"))
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            x = float(point[0])
            y = float(point[1])
        else:
            raise ValueError("polygon points must have x and y")
        if not (math.isfinite(x) and math.isfinite(y) and 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError("polygon points must be normalized between 0 and 1")
        points.append({"x": x, "y": y})
    return points


def _event_allowed_by_areas(event: dict, areas: list[list[dict]]) -> bool:
    if not areas:
        return True
    for box in _event_boxes(event):
        try:
            cx, cy = _box_center(box)
        except (KeyError, TypeError, ValueError):
            continue
        if _point_in_any_polygon(cx, cy, areas):
            return True
    return False


def _event_allowed_by_zone_policy(
    event: dict,
    areas: list[list[dict]],
    exclusions: list[list[dict]],
) -> bool:
    """Allow an event when at least one sampled box is included and not excluded.

    Exclusions suppress presentation only. Detection rows, tracks and footage
    remain intact, and an object that later leaves an exclusion can still form
    a surfaced event.
    """
    for box in _event_boxes(event):
        try:
            cx, cy = _box_center(box)
        except (KeyError, TypeError, ValueError):
            continue
        if exclusions and _point_in_any_polygon(cx, cy, exclusions):
            continue
        if not areas or _point_in_any_polygon(cx, cy, areas):
            return True
    return False


def _zone_filter_disabled(zone_id) -> bool:
    return zone_id is not None and str(zone_id).lower() in {"none", "off", "frame", "whole-frame"}


def _filter_with_polygons(events: list[dict], polygons: list[list[dict]]) -> list[dict]:
    if not polygons:
        return list(events)
    return [e for e in events if _event_allowed_by_areas(e, polygons)]


def _filter_with_zone_policy(
    events: list[dict],
    polygons: list[list[dict]],
    exclusions: list[list[dict]],
) -> list[dict]:
    if not polygons and not exclusions:
        return list(events)
    return [
        event for event in events
        if _event_allowed_by_zone_policy(event, polygons, exclusions)
    ]


def _point_in_any_polygon(x: float, y: float, polygons: list[list[dict]]) -> bool:
    return any(_point_in_polygon(x, y, polygon) for polygon in polygons)


def _point_in_polygon(x: float, y: float, polygon: list[dict]) -> bool:
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = float(polygon[i]["x"]), float(polygon[i]["y"])
        xj, yj = float(polygon[j]["x"]), float(polygon[j]["y"])
        if _point_on_segment(x, y, xi, yi, xj, yj):
            return True
        intersects = (yi > y) != (yj > y)
        if intersects:
            x_at_y = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x <= x_at_y:
                inside = not inside
        j = i
    return inside


def _point_on_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> bool:
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    if abs(cross) > 1e-9:
        return False
    dot = (px - ax) * (px - bx) + (py - ay) * (py - by)
    return dot <= 1e-9


class VideoWorker:
    """Continuous RTSP recorder — no detection trigger, pure archive + live HLS."""

    def __init__(self, source, video_dir: Path, db: VideoSegmentDB) -> None:
        self.source    = source
        self.video_dir = video_dir
        self.db        = db
        self._stop     = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._seg_id:    int | None  = None
        self._seg_path:  Path | None = None
        self._seg_start: float       = 0.0
        self._live_dir  = video_dir / "live" / source.id
        self._live_dir.mkdir(parents=True, exist_ok=True)
        self._status_lock = threading.Lock()
        self._last_segment_started_ts: float | None = None
        self._last_segment_completed_ts: float | None = None
        self._last_failure_ts: float | None = None
        self._last_failure_kind: str | None = None
        self._consecutive_failures = 0
        self._completed_segments = 0

    def status(self) -> dict:
        with self._status_lock:
            return {
                # The stamped stream contract is H.264 for both codec-copy and
                # re-encode modes. Reporting the invariant keeps media health
                # useful without opening a second RTSP connection to ffprobe.
                "codec": "h264",
                "segment_started_ts": self._last_segment_started_ts,
                "segment_completed_ts": self._last_segment_completed_ts,
                "last_failure_ts": self._last_failure_ts,
                "last_failure_kind": self._last_failure_kind,
                "consecutive_failures": self._consecutive_failures,
                "completed_segments": self._completed_segments,
            }

    def _record_failure(self, kind: str) -> None:
        now = time.time()
        with self._status_lock:
            self._last_failure_ts = now
            self._last_failure_kind = kind
            self._consecutive_failures += 1
            consecutive = self._consecutive_failures
        LOG.warning(
            "RECORDER-FAILURE src=%s kind=%s consecutive=%d",
            self.source.id, kind, consecutive,
        )

    def _record_segment_completed(self, ts: float, *, reset_failures: bool = True) -> None:
        with self._status_lock:
            self._last_segment_completed_ts = ts
            if reset_failures:
                self._consecutive_failures = 0
            self._completed_segments += 1

    def _live_playlist_segments(self) -> set[str]:
        playlist = self._live_dir / "live.m3u8"
        try:
            stat = playlist.stat()
            if time.time() - stat.st_mtime > _LIVE_HLS_STALE_PLAYLIST_SECONDS:
                try:
                    playlist.unlink()
                except OSError:
                    pass
                return set()
            lines = playlist.read_text(encoding="utf-8").splitlines()
        except OSError:
            return set()

        segments: set[str] = set()
        for line in lines:
            item = line.strip()
            if not item or item.startswith("#"):
                continue
            segments.add(Path(item.split("?", 1)[0]).name)
        return segments

    def _prune_live_dir(self) -> None:
        referenced = self._live_playlist_segments()
        cutoff = time.time() - _LIVE_HLS_UNREFERENCED_RETENTION_SECONDS
        for pattern in ("*.ts", "*.tmp"):
            for path in self._live_dir.glob(pattern):
                try:
                    if path.name in referenced:
                        continue
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except OSError:
                    pass

    def _salvage_orphan_segments(self) -> None:
        """Close out segments a previous crashed run left open.

        Fragmented recording means the file is valid up to its last complete
        fragment, so a crash costs at most one GOP: finalize the file, anchor
        it, close the row on the file's mtime (the last moment it was
        written). Rows touched within the last few seconds are skipped — that
        file may still be owned by a live ffmpeg (watchdog thread restart);
        they get salvaged on a later startup instead.
        """
        try:
            rows = self.db.open_segment_rows(self.source.id)
        except Exception:
            LOG.exception("salvage scan failed for %s", self.source.id)
            return
        for row in rows:
            path = self.video_dir / row["path"]
            try:
                mtime = path.stat().st_mtime
            except OSError:
                LOG.warning(
                    "salvage %s: open segment %s has no file — deleting row",
                    self.source.id, row["path"],
                )
                try:
                    self.db.delete_segment(row["id"])
                except Exception:
                    LOG.exception("salvage row delete failed: %s", row["path"])
                continue
            if time.time() - mtime < _SALVAGE_MIN_IDLE_SECONDS:
                continue
            try:
                finalized = _finalize_segment_file(path)
                media_epoch = _decode_media_epoch(path)
                if media_epoch is not None:
                    self.db.set_segment_media_start(row["id"], media_epoch)
                    _write_mp4_frame_clock(path)
                else:
                    # No readable clock: close the row but keep backfill away,
                    # mirroring the unreadable-marker close path.
                    self.db.mark_scanned(row["id"])
                self.db.close_segment(row["id"], mtime, None, None)
                LOG.warning(
                    "salvaged crashed segment seg=%d src=%s path=%s "
                    "finalized=%s anchored=%s",
                    row["id"], self.source.id, row["path"],
                    finalized, media_epoch is not None,
                )
            except Exception:
                LOG.exception("salvage failed for %s", row["path"])

    def run(self) -> None:
        """Continuous recording loop — call from a daemon thread."""
        LOG.info("continuous recording started: %s", self.source.id)
        self._salvage_orphan_segments()
        backoff = _RECORD_RETRY_INITIAL_SECONDS
        while not self._stop.is_set():
            try:
                ts = time.time()
                self._start_segment(ts)
                if self._proc:
                    early_stderr = ""
                    returncode = None
                    # Poll every 5s so we detect ffmpeg exit within 5 seconds
                    deadline = time.time() + _MAX_SEGMENT_SECONDS
                    while time.time() < deadline and not self._stop.is_set():
                        if self._proc.poll() is not None:
                            err = b""
                            try:
                                err = self._proc.stderr.read() or b""
                            except Exception:
                                pass
                            early_stderr = err.decode("utf-8", "replace")
                            returncode = self._proc.returncode
                            LOG.warning("ffmpeg exited early for %s (rc=%s): %s",
                                        self.source.id, returncode,
                                        early_stderr[-1200:])
                            break
                        if self._seg_id and not getattr(self, "_anchor_set", False):
                            self._observe_marker_anchor()
                        self._stop.wait(_RECORD_POLL_SECONDS)
                    elapsed = time.time() - ts
                    stopped_ts = time.time()
                    segment_ok = self._stop_segment(stopped_ts)
                    clean_epoch_end = returncode == 0 and segment_ok
                    if returncode is not None:
                        if clean_epoch_end:
                            LOG.info(
                                "recording epoch ended cleanly for %s; "
                                "starting the next available epoch",
                                self.source.id,
                            )
                            self._record_segment_completed(stopped_ts)
                        elif segment_ok:
                            self._record_segment_completed(
                                stopped_ts, reset_failures=False,
                            )
                        if not clean_epoch_end:
                            self._record_failure("ffmpeg_early_exit")
                    elif segment_ok:
                        self._record_segment_completed(stopped_ts)
                    else:
                        self._record_failure("empty_segment")
                    # Reset backoff after a segment ran long enough to be healthy.
                    backoff = (
                        _RECORD_RETRY_INITIAL_SECONDS
                        if clean_epoch_end
                        or (elapsed > _RECORD_SUCCESS_SECONDS and segment_ok)
                        else min(backoff * 2, _RECORD_RETRY_MAX_SECONDS)
                    )
                    if not clean_epoch_end and (
                        elapsed <= _RECORD_SUCCESS_SECONDS or not segment_ok
                    ):
                        LOG.warning("short segment (%.0fs) for %s — backoff %.0fs",
                                    elapsed, self.source.id, backoff)
                        self._stop.wait(backoff)
                else:
                    LOG.warning("ffmpeg failed to start for %s — backoff %.0fs",
                                self.source.id, backoff)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, _RECORD_RETRY_MAX_SECONDS)
            except Exception:
                LOG.exception(
                    "recording error for %s — retry in %.0fs",
                    self.source.id,
                    _RECORD_EXCEPTION_RETRY_SECONDS,
                )
                self._stop.wait(_RECORD_EXCEPTION_RETRY_SECONDS)
        LOG.info("continuous recording stopped: %s", self.source.id)

    def stop(self) -> None:
        self._stop.set()
        if self._seg_id or self._proc:
            self._stop_segment(time.time())

    def _start_segment(self, ts: float) -> None:
        from .capture import resolve_rtsp_url
        url    = resolve_rtsp_url(self.source)
        ffmpeg = shutil.which("ffmpeg")
        if not url or not ffmpeg:
            self._record_failure("recorder_unavailable")
            return
        if not self._wait_for_relay_path(url):
            self._record_failure("relay_unavailable")
            return
        dt       = datetime.fromtimestamp(ts, tz=timezone.utc)
        date_dir = self.video_dir / self.source.id / dt.strftime("%Y/%m/%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        self._prune_live_dir()
        seg_path = date_dir / dt.strftime("%Y-%m-%d_%H-%M-%S.mp4")
        rel_path = seg_path.relative_to(self.video_dir).as_posix()
        try:
            self._proc = subprocess.Popen(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
                 # Preserve the stamper's rtp-based pts as the MP4/HLS container
                 # timeline. -use_wallclock_as_timestamps would overwrite it with
                 # bursty recorder arrival time, compressing the early frames so
                 # the player's currentTime no longer maps to the frame clock
                 # (the box-lead bug). The stamper already supplies clean
                 # monotonic pts that track the SEI clock.
                 "-rtsp_transport", self.source.rtsp_transport,
                 "-i", url,
                 # Archive: fragmented MP4 while recording — moov up front,
                 # one self-contained fragment per keyframe. The open file is
                 # readable/probe-able at any instant and a crash loses at
                 # most one GOP instead of the whole segment (faststart wrote
                 # the index only at finalize, so a killed writer left an
                 # unreadable blob). Sealed segments are remuxed to classic
                 # faststart in _stop_segment for byte-range seeking.
                 # flush_packets: fragments hit the disk as they complete
                 # instead of sitting in the 32KB avio buffer — without it a
                 # crash can still eat several seconds at low bitrates.
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "64k",
                 "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                 "-flush_packets", "1",
                 str(seg_path),
                 # Live: rolling HLS. ffmpeg deletes the active window; startup
                 # pruning clears unreferenced files from older ffmpeg writers.
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "64k",
                 "-f", "hls",
                 "-hls_time", str(_LIVE_HLS_SEGMENT_SECONDS),
                 "-hls_list_size", str(_LIVE_HLS_LIST_SIZE),
                 "-hls_start_number_source", "epoch",
                 "-hls_flags", "delete_segments+omit_endlist+temp_file+program_date_time",
                 "-hls_segment_filename", str(self._live_dir / "seg_%010d.ts"),
                 str(self._live_dir / "live.m3u8"),
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
        except Exception:
            LOG.exception("failed to start ffmpeg for %s", self.source.id)
            self._proc = None
            self._record_failure("ffmpeg_start_failed")
            return
        self._seg_path  = seg_path
        self._seg_start = ts
        with self._status_lock:
            self._last_segment_started_ts = ts
        try:
            self._seg_id = self.db.open_segment(self.source.id, rel_path, ts)
        except Exception:
            self._seg_id = None
            LOG.exception("failed to open segment row for %s", self.source.id)
            self._record_failure("database_open_failed")
        self._anchor_set = False
        LOG.info("segment started: %s", rel_path)

    def _observe_marker_anchor(self) -> None:
        """Early open-segment anchor: set media_epoch provisionally so the
        detector stops queueing rows for the whole open segment. The recording
        is fragmented MP4, readable while open, so decode the segment file
        itself on the same container-pts basis as the authoritative close-time
        anchor (which still overwrites this after the faststart remux). Live
        HLS fragments remain as fallback for a not-yet-readable file.
        """
        seg_id = self._seg_id
        seg_start = self._seg_start
        if not seg_id or not seg_start:
            return
        media_epoch = None
        if self._seg_path is not None:
            media_epoch = _decode_media_epoch(self._seg_path)
        if media_epoch is None:
            try:
                fragments = sorted(
                    (p for p in self._live_dir.glob("seg_*.ts")
                     if p.stat().st_mtime >= seg_start - 1.0),
                    key=lambda p: p.stat().st_mtime,
                )
            except OSError:
                return
            if not fragments:
                return
            media_epoch = _decode_media_epoch(
                fragments[0],
                playback_zero=_CLOCK_ZERO_FIRST_FRAME,
            )
        if media_epoch is None:
            return
        try:
            self.db.set_segment_media_start(seg_id, media_epoch)
        except Exception:
            LOG.exception("early anchor set failed for %s", self.source.id)
            return
        self._anchor_set = True
        LOG.info("CLOCK-ANCHOR-EARLY seg=%d src=%s media_epoch=%.3f start_ts%+.3f",
                 seg_id, self.source.id, media_epoch, media_epoch - seg_start)

    def _stop_segment(self, ts: float) -> bool:
        proc      = self._proc;     self._proc     = None
        seg_path  = self._seg_path; self._seg_path = None
        seg_id    = self._seg_id
        seg_start = self._seg_start
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM); proc.wait(timeout=10)
            except Exception:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
        if proc and proc.stderr:
            try:
                proc.stderr.close()
            except OSError:
                pass
        self._seg_id = None
        self._seg_start = 0.0
        if seg_id:
            if seg_path:
                try:
                    file_size = seg_path.stat().st_size
                except OSError:
                    file_size = 0
                if file_size <= 0:
                    LOG.warning(
                        "discarding failed segment seg=%d src=%s path=%s: no MP4 produced",
                        seg_id, self.source.id, seg_path,
                    )
                    self.db.delete_segment(seg_id)
                    try:
                        seg_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        LOG.exception("failed to remove empty segment: %s", seg_path)
                    return False

                _finalize_segment_file(seg_path)
                media_epoch = _decode_media_epoch(seg_path)
                if media_epoch is not None:
                    self.db.set_segment_media_start(seg_id, media_epoch)
                    _write_mp4_frame_clock(seg_path)
                    LOG.info(
                        "CLOCK-ANCHOR seg=%d src=%s media_epoch=%.3f start_ts%+.3f",
                        seg_id, self.source.id, media_epoch, media_epoch - seg_start,
                    )
                else:
                    LOG.warning(
                        "CLOCK-ANCHOR seg=%d src=%s no readable frame-0 clock; "
                        "marking invalid so backfill does not queue it",
                        seg_id, self.source.id,
                    )
                    self.db.mark_scanned(seg_id)
            self.db.close_segment(seg_id, ts, None, None)
            return True
        return False

    def _wait_for_relay_path(self, url: str) -> bool:
        relay_host = os.environ.get("WANYARD_RELAY_HOST", "").strip()
        if not relay_host:
            return True
        parsed = urlparse(url)
        path = parsed.path.lstrip("/")
        if not path or parsed.hostname != relay_host:
            return True
        try:
            timeout = float(os.environ.get(
                "WANYARD_RECORD_WAIT_FOR_RELAY_SECONDS",
                str(_RECORD_RELAY_WAIT_SECONDS),
            ))
        except (TypeError, ValueError):
            timeout = _RECORD_RELAY_WAIT_SECONDS
        if timeout <= 0:
            return True

        deadline = time.time() + timeout
        logged = False
        while not self._stop.is_set():
            if _mediamtx_path_ready(relay_host, path):
                if logged:
                    LOG.info("relay path ready for %s: %s", self.source.id, path)
                return True
            if not logged:
                LOG.info(
                    "waiting for relay path before recording %s: %s",
                    self.source.id, path,
                )
                logged = True
            remaining = deadline - time.time()
            if remaining <= 0:
                LOG.warning(
                    "relay path not ready after %.0fs for %s: %s",
                    timeout, self.source.id, path,
                )
                return False
            self._stop.wait(min(1.0, remaining))
        return False


def _finalize_segment_file(path: Path, *, timeout: float = 300.0) -> bool:
    """Remux a fragmented recording into a classic faststart MP4, atomically.

    Recording writes fragmented MP4 (crash-safe, readable while open), but
    browsers seek unindexed fragmented files poorly over HTTP ranges — they
    fetch sequentially hunting for the target. Sealed archives therefore get
    the moov-up-front layout this recorder always produced. Stream copy, all
    streams mapped, timestamps preserved; the close-time anchor is computed
    AFTER this remux, on the exact file browsers will play.

    On any failure the fragmented original stays in place — playable, worse
    seeking. Footage is never sacrificed to a failed polish step.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    tmp = path.with_name(path.name + ".faststart.tmp")
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(path), "-map", "0", "-c", "copy",
             "-movflags", "+faststart", "-f", "mp4", str(tmp)],
            capture_output=True, timeout=timeout,
        )
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            LOG.warning(
                "faststart remux failed for %s (rc=%s): %s — keeping "
                "fragmented file", path, proc.returncode,
                proc.stderr.decode("utf-8", "replace")[-500:],
            )
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(path)
        return True
    except Exception:
        LOG.exception("faststart remux failed for %s — keeping fragmented file",
                      path)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _frame_clock_path(path: Path) -> Path:
    return path.with_name(path.name + ".clock.json")


def _write_mp4_frame_clock(path: Path) -> Path | None:
    """Persist finalized-MP4 presentation PTS -> SEI clock mappings.

    Packet scanning is compressed-domain work: no video decode or re-encode.
    The browser matches requestVideoFrameCallback.mediaTime against these exact
    PTS values, recovering the SEI belonging to the frame it actually painted.
    """
    sidecar = _frame_clock_path(path)
    try:
        media_stat = path.stat()
        if sidecar.is_file() and sidecar.stat().st_mtime_ns >= media_stat.st_mtime_ns:
            return sidecar
    except OSError:
        return None

    container = None
    try:
        import av

        container = av.open(str(path))
        stream = next((s for s in container.streams if s.type == "video"), None)
        if stream is None:
            return None
        by_pts: dict[int, tuple[float, int]] = {}
        for packet in container.demux(stream):
            if packet.pts is None or packet.time_base is None or not packet.size:
                continue
            value = sei.extract(bytes(packet))
            if value is None:
                continue
            pts = float(packet.pts * packet.time_base)
            if not math.isfinite(pts):
                continue
            by_pts[round(pts * 1_000_000)] = (pts, value)
        frames = [[round(pts, 9), value] for pts, value in sorted(by_pts.values())]
        # A SEI-less file (re-encode segments, legacy archives) persists an
        # EMPTY sidecar as a negative cache: the mtime guard above then
        # answers every later request without touching the media. Deleting it
        # instead meant each seek into old footage re-scanned the whole MP4 —
        # multi-second stalls on every random timeline seek until the file
        # aged out. The frontend treats empty frames as "no clock" (fallback).
        payload = {
            "version": _FRAME_CLOCK_VERSION,
            "frames": frames,
        }
        tmp = sidecar.with_name(f".{sidecar.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            tmp.replace(sidecar)
        finally:
            tmp.unlink(missing_ok=True)
        LOG.info("MP4-FRAME-CLOCK path=%s frames=%d", path, len(frames))
        return sidecar
    except Exception:
        LOG.warning("failed to build MP4 frame clock for %s", path, exc_info=True)
        return None
    finally:
        if container is not None:
            try:
                container.close()
            except Exception:
                pass


def _decode_media_epoch(
    path: Path | str,
    *,
    playback_zero: str = _CLOCK_ZERO_CONTAINER_PTS,
) -> float | None:
    """Decode early clock frames and return the segment's media_epoch.

    clock is the only absolute time. This function never treats container PTS,
    filenames, or wall clock as truth; it reads the clock SEI from the
    pixels, then subtracts the player-relative offset where that exact frame is
    shown. The result is the clock/Unix time for player currentTime == 0.

    Closed MP4 files are played directly by the browser, and browser
    currentTime follows the MP4 container PTS. If the first readable sample
    starts at a positive PTS, preserving that offset is required so
    media_epoch + currentTime equals the frame's clock SEI.

    The open live HLS anchor is provisional only: before the MP4 exists there is
    no final MP4 player origin to calibrate against. It uses the first readable
    frame as temporary offset zero to unblock live detection rows; the sealed
    MP4 replaces it with the authoritative clock->player anchor.
    """
    if playback_zero not in {_CLOCK_ZERO_CONTAINER_PTS, _CLOCK_ZERO_FIRST_FRAME}:
        raise ValueError(f"unknown clock playback zero: {playback_zero}")
    try:
        import av
        import statistics

        container = av.open(str(path))
        try:
            video_stream = next((s for s in container.streams if s.type == "video"), None)
            if video_stream is None:
                return None
            first_pts: float | None = None
            samples: list[float] = []
            scanned = 0
            done = False
            for packet in container.demux(video_stream):
                if done:
                    break
                for frame in packet.decode():
                    frame_pts = _frame_pts_seconds(frame)
                    if first_pts is None and frame_pts is not None:
                        first_pts = frame_pts
                    # The clock rides as SEI side data for every stamped
                    # segment (sei_copy and reencode alike).
                    clock_ts, crc_ok = sei.decode_frame(frame)
                    if crc_ok and clock_ts is not None:
                        if frame_pts is None:
                            if samples:
                                continue
                            player_offset = 0.0
                        elif playback_zero == _CLOCK_ZERO_CONTAINER_PTS:
                            player_offset = frame_pts
                        elif first_pts is not None:
                            player_offset = frame_pts - first_pts
                        else:
                            continue
                        samples.append(clock_ts - player_offset)
                    scanned += 1
                    if scanned >= _CLOCK_ANCHOR_SCAN_FRAMES or (
                        first_pts is not None and frame_pts is not None
                        and frame_pts - first_pts >= _CLOCK_ANCHOR_SCAN_SECONDS
                    ):
                        done = True
                        break
            return statistics.median(samples) if samples else None
        finally:
            container.close()
    except Exception:
        LOG.debug("failed to read first-frame clock SEI from %s", path, exc_info=True)
        return None


def _frame_pts_seconds(frame) -> float | None:
    pts = getattr(frame, "pts", None)
    time_base = getattr(frame, "time_base", None)
    if pts is None or time_base is None:
        return None
    try:
        return float(pts * time_base)
    except (TypeError, ValueError):
        return None


def _mediamtx_path_ready(relay_host: str, path: str) -> bool:
    url = f"http://{relay_host}:9997/v3/paths/list"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False
    for item in payload.get("items") or []:
        if item.get("name") == path:
            return bool(item.get("ready"))
    return False


def _write_webvtt(path: Path, sprite_name: str, w: int, h: int,
                  cols: int, fps_str: str) -> None:
    num, den = (int(x) for x in fps_str.split("/"))
    interval = den / num
    lines = ["WEBVTT", ""]
    for tile in range(cols * 6):
        col, row = tile % cols, tile // cols
        t = tile * interval
        lines += [f"{_vtt_time(t)} --> {_vtt_time(t+interval)}",
                  f"{sprite_name}#xywh={col*w},{row*h},{w},{h}", ""]
    path.write_text("\n".join(lines))


def _vtt_time(s: float) -> str:
    h = int(s // 3600); m = int((s % 3600) // 60); s = s % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"
