from __future__ import annotations

import json
import logging
import math
import os
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger(__name__)

_MAX_SEGMENT_SECONDS = 600
_LIVE_HLS_SEGMENT_SECONDS = 2
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
_PROVISIONAL_GRACE_SECONDS = 3600.0
_OBJECT_TRACK_CENTER_DISTANCE = 0.045
_OBJECT_TRACK_AREA_RATIO = 3.0
_OBJECT_MIN_OBSERVATIONS = 2
_OBJECT_EXIT_GRACE_SECONDS = 15 * 60.0
_OBJECT_TRACK_LOOKBACK_SECONDS = 2 * 60 * 60.0
_CLASS_PRIORITY      = ["person", "bird", "cat", "dog",
                         "bus", "truck", "motorcycle", "bicycle", "car",
                         "backpack", "suitcase"]

_DDL = """
CREATE TABLE IF NOT EXISTS segments (
    id              INTEGER PRIMARY KEY,
    source_id       TEXT    NOT NULL,
    path            TEXT    NOT NULL UNIQUE,
    start_ts        REAL    NOT NULL,
    end_ts          REAL,
    actual_start_ts REAL,        -- camera-accurate first-frame time (from HLS)
    spritesheet     TEXT,
    webvtt          TEXT
);
CREATE INDEX IF NOT EXISTS seg_source_ts ON segments(source_id, start_ts);
CREATE INDEX IF NOT EXISTS seg_source_end_ts ON segments(source_id, end_ts, start_ts);

CREATE TABLE IF NOT EXISTS video_detections (
    id          INTEGER PRIMARY KEY,
    segment_id  INTEGER REFERENCES segments(id) ON DELETE CASCADE,
    ts_offset   REAL    NOT NULL,
    has_human   INTEGER NOT NULL DEFAULT 0,
    confidence  REAL    NOT NULL DEFAULT 0,
    boxes_json  TEXT,
    classes_json TEXT
);
CREATE INDEX IF NOT EXISTS vdet_seg ON video_detections(segment_id, ts_offset);

CREATE TABLE IF NOT EXISTS video_events (
    id          INTEGER PRIMARY KEY,
    segment_id  INTEGER REFERENCES segments(id) ON DELETE CASCADE,
    source_id   TEXT    NOT NULL,
    abs_ts      REAL    NOT NULL,
    class       TEXT    NOT NULL,
    start_off   REAL    NOT NULL,
    end_off     REAL    NOT NULL,
    confidence  REAL    NOT NULL DEFAULT 0,
    boxes_json  TEXT
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
    class       TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,
    start_off   REAL    NOT NULL DEFAULT 0,
    end_off     REAL    NOT NULL DEFAULT 0,
    confidence  REAL    NOT NULL DEFAULT 0,
    boxes_json  TEXT
);
CREATE INDEX IF NOT EXISTS oevt_source_ts ON object_events(source_id, abs_ts);
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

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Real-time detections from HLS .ts segments, pending MP4 backfill.
-- Rows here make events appear in the UI within seconds of detection.
-- Consumed and deleted by _backfill_loop when the MP4 segment closes.
CREATE TABLE IF NOT EXISTS hls_events (
    id          INTEGER PRIMARY KEY,
    source_id   TEXT    NOT NULL,
    abs_ts      REAL    NOT NULL,
    class       TEXT    NOT NULL,
    confidence  REAL    NOT NULL DEFAULT 0,
    boxes_json  TEXT,
    thumb_jpeg  BLOB,
    created_at  REAL    NOT NULL DEFAULT (unixepoch('now'))
);
CREATE INDEX IF NOT EXISTS hevt_source_ts ON hls_events(source_id, abs_ts);
"""



def _dominant_class(classes: list[str]) -> str:
    for c in _CLASS_PRIORITY:
        if c in classes:
            return c
    return classes[0] if classes else "unknown"



class VideoSegmentDB:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_DDL)
            # Additive migrations — safe to re-run, fail silently if column exists
            for migration in [
                "ALTER TABLE hls_events ADD COLUMN thumb_jpeg BLOB",
                "ALTER TABLE segments  ADD COLUMN actual_start_ts REAL",
                "ALTER TABLE video_events ADD COLUMN event_type TEXT NOT NULL DEFAULT 'detection'",
                "ALTER TABLE video_events ADD COLUMN track_id TEXT",
            ]:
                try:
                    conn.execute(migration)
                except Exception:
                    pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

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
            conn.execute(
                "UPDATE segments SET end_ts=?, spritesheet=?, webvtt=? WHERE id=?",
                (end_ts, spritesheet, webvtt, segment_id),
            )

    def add_detection(self, segment_id: int, ts_offset: float,
                      has_human: bool, confidence: float,
                      boxes: list | None, classes: list | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO video_detections"
                "(segment_id, ts_offset, has_human, confidence, boxes_json, classes_json)"
                " VALUES(?,?,?,?,?,?)",
                (segment_id, ts_offset, int(has_human), confidence,
                 json.dumps(boxes) if boxes else None,
                 json.dumps(classes) if classes else None),
            )

    def replace_detections(self, segment_id: int, detections: list[dict]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM video_detections WHERE segment_id=?", (segment_id,))
            conn.executemany(
                "INSERT INTO video_detections"
                "(segment_id, ts_offset, has_human, confidence, boxes_json, classes_json)"
                " VALUES(?,?,?,?,?,?)",
                [
                    (
                        segment_id,
                        d["ts_offset"],
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
                "SELECT ts_offset, has_human, confidence, boxes_json, classes_json"
                " FROM video_detections WHERE segment_id=? ORDER BY ts_offset",
                (segment_id,),
            ).fetchall()
        return [{
            "ts_offset":  r["ts_offset"],
            "has_human":  bool(r["has_human"]),
            "confidence": r["confidence"],
            "boxes":   json.loads(r["boxes_json"])   if r["boxes_json"]   else [],
            "classes": json.loads(r["classes_json"]) if r["classes_json"] else [],
        } for r in rows]

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
        sanitized = [_sanitize_zone(source_id, z) for z in zones]
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM video_zones WHERE source_id=?", (source_id,))
            conn.executemany(
                "INSERT INTO video_zones"
                " (source_id, name, zone_type, polygon_json, enabled, created_at, updated_at)"
                " VALUES(:source_id,:name,:zone_type,:polygon_json,:enabled,:created_at,:updated_at)",
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

    def activity_areas(self, source_id: str) -> list[list[dict]]:
        return [
            z["polygon"] for z in self.list_zones(source_id)
            if z["enabled"] and len(z["polygon"]) >= 3
        ]

    def has_activity_areas(self, source_id: str | None = None) -> bool:
        return any(
            z["enabled"] and len(z["polygon"]) >= 3
            for z in self.list_zones(source_id)
        )

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
            if _event_allowed_by_areas(event, area_cache[source_id]):
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
                if z["id"] == z_id and z["enabled"] and len(z["polygon"]) >= 3:
                    return [z["polygon"]]
        return self.activity_areas(source_id) if source_id else []

    def object_events_available(
        self,
        source_id: str | None = None,
        since: float | None = None,
        until: float | None = None,
    ) -> bool:
        with self._connect() as conn:
            if source_id and source_id != "all":
                row = conn.execute(
                    "SELECT since, until FROM object_derivations WHERE source_id=?",
                    (source_id,),
                ).fetchone()
                return _derivation_covers(dict(row) if row else None, since, until)

            where, params = ["1"], []
            if since is not None:
                where.append("(end_ts IS NULL OR end_ts>=?)")
                params.append(since)
            if until is not None:
                where.append("start_ts<=?")
                params.append(until)
            sources = [
                r["source_id"] for r in conn.execute(
                    "SELECT DISTINCT source_id FROM segments"
                    f" WHERE {' AND '.join(where)}",
                    params,
                ).fetchall()
            ]
            if not sources:
                return False
            rows = {
                r["source_id"]: dict(r)
                for r in conn.execute(
                    "SELECT source_id, since, until FROM object_derivations"
                ).fetchall()
            }
        return all(_derivation_covers(rows.get(src), since, until) for src in sources)

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
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO object_events"
                "(track_id, segment_id, source_id, abs_ts, class, event_type,"
                " start_off, end_off, confidence, boxes_json)"
                " VALUES(:track_id,:segment_id,:source_id,:abs_ts,:class,:event_type,"
                " :start_off,:end_off,:confidence,:boxes_json)",
                events,
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
            where.append("e.abs_ts>=?"); params.append(since)
        if until is not None:
            where.append("e.abs_ts<=?"); params.append(until)
        if date:
            import calendar
            from datetime import date as ddate
            d = ddate.fromisoformat(date)
            lo = calendar.timegm(d.timetuple()) - 86400
            hi = lo + 3 * 86400
            where.append("e.abs_ts BETWEEN ? AND ?")
            params += [lo, hi]
        polygons = self.zone_polygons(source_id, zone_id)
        query_limit = limit
        if polygons and limit < 100000:
            query_limit = max(limit * 20, 200)
        sql = (
            "SELECT e.*, s.path as seg_path, s.spritesheet, s.start_ts as seg_start_ts"
            " FROM object_events e LEFT JOIN segments s ON s.id=e.segment_id"
            f" WHERE {' AND '.join(where)}"
            " ORDER BY e.abs_ts DESC LIMIT ?"
        )
        params.append(query_limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        events = _filter_with_polygons([dict(r) for r in rows], polygons)[:limit]
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
            " s.start_ts as seg_start_ts"
            " FROM object_events e LEFT JOIN segments s ON s.id=e.segment_id"
            f" WHERE {base}"
        )
        polygons = self.zone_polygons(source_id, zone_id)
        query_limit = max(limit * 20, 200) if polygons else limit
        with self._connect() as conn:
            before = conn.execute(
                f"{select} AND e.abs_ts<=? ORDER BY e.abs_ts DESC LIMIT ?",
                (*params, around, query_limit),
            ).fetchall()
            after = conn.execute(
                f"{select} AND e.abs_ts>? ORDER BY e.abs_ts ASC LIMIT ?",
                (*params, around, query_limit),
            ).fetchall()
        rows = _filter_with_polygons([dict(r) for r in before] + [dict(r) for r in after], polygons)
        rows.sort(key=lambda r: (abs(r["abs_ts"] - around), r["abs_ts"]))
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

    def track_object_events(self, segment: dict, tracklets: list[dict]) -> list[dict]:
        source_id = segment["source_id"]
        seg_start = float(segment["start_ts"])
        seg_end = float(segment.get("end_ts") or seg_start)
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
                box = _event_box(tracklet)
                if not box:
                    continue
                cx, cy = _box_center(box)
                area = _box_area(box)
                best: dict | None = None
                best_dist = _OBJECT_TRACK_CENTER_DISTANCE
                for track in active:
                    if int(track["id"]) in used_track_ids:
                        continue
                    if track["class"] != tracklet["class"]:
                        continue
                    if not _area_compatible(area, float(track["area"])):
                        continue
                    dist = _center_distance(cx, cy, float(track["cx"]), float(track["cy"]))
                    if dist <= best_dist:
                        best = track
                        best_dist = dist

                last_seen = float(tracklet["abs_ts"]) + max(
                    0.0, float(tracklet["end_off"]) - float(tracklet["start_off"])
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
                            cx, cy, area, last_seen, tracklet["segment_id"],
                            tracklet["start_off"], tracklet["end_off"],
                            tracklet["confidence"], observations,
                            tracklet["boxes_json"], track_id,
                        ),
                    )
                    best.update({
                        "cx": cx,
                        "cy": cy,
                        "area": area,
                        "last_seen": last_seen,
                        "last_segment_id": tracklet["segment_id"],
                        "last_start_off": tracklet["start_off"],
                        "last_end_off": tracklet["end_off"],
                        "confidence": tracklet["confidence"],
                        "observations": observations,
                        "boxes_json": tracklet["boxes_json"],
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
                            source_id, tracklet["class"], cx, cy, area,
                            tracklet["abs_ts"], last_seen, tracklet["segment_id"],
                            tracklet["segment_id"], tracklet["start_off"],
                            tracklet["start_off"], tracklet["end_off"],
                            tracklet["confidence"], int(tracklet.get("observations", 1)),
                            tracklet["boxes_json"], tracklet["abs_ts"],
                        ),
                    )
                    track_id = int(cur.lastrowid)
                    used_track_ids.add(track_id)
                    active.append({
                        "id": track_id,
                        "source_id": source_id,
                        "class": tracklet["class"],
                        "cx": cx,
                        "cy": cy,
                        "area": area,
                        "first_seen": tracklet["abs_ts"],
                        "last_seen": last_seen,
                        "first_segment_id": tracklet["segment_id"],
                        "last_segment_id": tracklet["segment_id"],
                        "first_start_off": tracklet["start_off"],
                        "last_start_off": tracklet["start_off"],
                        "last_end_off": tracklet["end_off"],
                        "confidence": tracklet["confidence"],
                        "observations": int(tracklet.get("observations", 1)),
                        "boxes_json": tracklet["boxes_json"],
                        "active": 1,
                        "state": "active",
                        "stationary_since": tracklet["abs_ts"],
                    })
                    output.append({
                        **tracklet,
                        "event_type": "appeared",
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
        if polygons and limit < 100000:
            query_limit = max(limit * 20, 200)
        sql = (
            "SELECT e.*, s.path as seg_path, s.spritesheet, s.start_ts as seg_start_ts"
            " FROM video_events e JOIN segments s ON s.id=e.segment_id"
            f" WHERE {' AND '.join(where)}"
            " ORDER BY e.abs_ts DESC LIMIT ?"
        )
        params.append(query_limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return _filter_with_polygons([dict(r) for r in rows], polygons)[:limit]

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
            " s.start_ts as seg_start_ts"
            " FROM video_events e JOIN segments s ON s.id=e.segment_id"
            f" WHERE {base}"
        )
        polygons = self.zone_polygons(source_id, zone_id)
        query_limit = max(limit * 20, 200) if polygons else limit
        with self._connect() as conn:
            before = conn.execute(
                f"{select} AND e.abs_ts<=? ORDER BY e.abs_ts DESC LIMIT ?",
                (*params, around, query_limit),
            ).fetchall()
            after = conn.execute(
                f"{select} AND e.abs_ts>? ORDER BY e.abs_ts ASC LIMIT ?",
                (*params, around, query_limit),
            ).fetchall()
        rows = _filter_with_polygons([dict(r) for r in before] + [dict(r) for r in after], polygons)
        rows.sort(key=lambda r: (abs(r["abs_ts"] - around), r["abs_ts"]))
        return rows[:limit]

    def get_event_with_segment(self, event_id) -> dict | None:
        raw_id = str(event_id)
        if raw_id.startswith("o:"):
            try:
                object_event_id = int(raw_id[2:])
            except ValueError:
                return None
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT e.*, s.path as seg_path, s.start_ts as seg_start_ts,"
                    " s.end_ts as seg_end_ts"
                    " FROM object_events e JOIN segments s ON s.id=e.segment_id"
                    " WHERE e.id=?",
                    (object_event_id,),
                ).fetchone()
            return dict(row) if row else None

        try:
            legacy_event_id = int(raw_id)
        except (TypeError, ValueError):
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT e.*, s.path as seg_path, s.start_ts as seg_start_ts,"
                " s.end_ts as seg_end_ts"
                " FROM video_events e JOIN segments s ON s.id=e.segment_id"
                " WHERE e.id=?",
                (legacy_event_id,),
            ).fetchone()
        return dict(row) if row else None

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
        if polygons:
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
            for event in _filter_with_polygons([dict(r) for r in rows], polygons):
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
            where.append("abs_ts>=?"); params.append(since)
        if until is not None:
            where.append("abs_ts<?"); params.append(until)
        polygons = self.zone_polygons(source_id, zone_id)
        if polygons:
            sql = (
                f"SELECT source_id, class, boxes_json FROM {table}"
                f" WHERE {' AND '.join(where)}"
            )
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
            classes: dict[str, int] = {}
            for event in _filter_with_polygons([dict(r) for r in rows], polygons):
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

    def list_segments(self, source_id: str | None = None) -> list[dict]:
        where, params = [], []
        if source_id and source_id != "all":
            where.append("source_id=?"); params.append(source_id)
        sql = "SELECT * FROM segments"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY start_ts DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def segments_overlapping(self, source_id: str | None,
                             start_ts: float, end_ts: float) -> list[dict]:
        where, params = [
            "end_ts IS NOT NULL",
            "end_ts>?",
            "start_ts<?",
        ], [start_ts, end_ts]
        if source_id and source_id != "all":
            where.append("source_id=?"); params.append(source_id)
        sql = (
            "SELECT * FROM segments"
            f" WHERE {' AND '.join(where)}"
            " ORDER BY start_ts"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def provisional_events(self, source_id: str | None = None,
                           since: float | None = None,
                           zone_id=None) -> list[dict]:
        cutoff = time.time() - _PROVISIONAL_GRACE_SECONDS
        open_cutoff = time.time() - _LIVE_HLS_UNREFERENCED_RETENTION_SECONDS
        where, params = [
            "((s.end_ts IS NULL AND s.start_ts>=?)"
            " OR (s.end_ts IS NOT NULL AND s.end_ts>=?"
            " AND NOT EXISTS (SELECT 1 FROM video_events e WHERE e.segment_id=s.id)))"
        ], [open_cutoff, cutoff]
        if source_id and source_id != "all":
            where.append("s.source_id=?"); params.append(source_id)
        if since is not None:
            where.append("s.start_ts>=?"); params.append(since - _MAX_SEGMENT_SECONDS)
        sql = (
            "SELECT s.* FROM segments s"
            f" WHERE {' AND '.join(where)}"
            " ORDER BY s.start_ts DESC"
        )
        with self._connect() as conn:
            segs = [dict(r) for r in conn.execute(sql, params).fetchall()]

        polygons = self.zone_polygons(source_id, zone_id)
        events: list[dict] = []
        for seg in segs:
            rows = _object_tracklets_from_detections(seg, self.detections_for_segment(seg["id"]))
            for row in rows:
                if since is not None and row["abs_ts"] < since:
                    continue
                row["id"] = f"p:{row['segment_id']}:{row['class']}:{row['start_off']:.1f}"
                row["provisional"] = True
                row["seg_path"] = seg["path"]
                row["spritesheet"] = seg.get("spritesheet")
                row["seg_start_ts"] = seg["start_ts"]
                events.append(row)
        events = _filter_with_polygons(events, polygons)
        # Merge real-time HLS events (tagged within seconds of capture)
        hls = self.get_hls_events(source_id=source_id, since=since, zone_id=zone_id)
        events.extend(hls)
        events.sort(key=lambda r: r["abs_ts"], reverse=True)
        return events

    # ── HLS real-time event store ──────────────────────────────────────────
    def insert_hls_events(self, events: list[dict]) -> None:
        """Store provisional events detected from live HLS .ts segments."""
        area_cache: dict[str, list[list[dict]]] = {}
        filtered: list[dict] = []
        for event in events:
            source_id = event.get("source_id")
            if not source_id:
                continue
            if source_id not in area_cache:
                area_cache[source_id] = self.activity_areas(source_id)
            if _event_allowed_by_areas(event, area_cache[source_id]):
                filtered.append(event)
        if not filtered:
            return
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO hls_events(source_id, abs_ts, class, confidence, boxes_json, thumb_jpeg)"
                " VALUES(:source_id,:abs_ts,:class,:confidence,:boxes_json,:thumb_jpeg)",
                filtered,
            )

    def get_hls_thumb(self, hls_event_id: int) -> bytes | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT thumb_jpeg FROM hls_events WHERE id=?", (hls_event_id,)
            ).fetchone()
        return bytes(row["thumb_jpeg"]) if row and row["thumb_jpeg"] else None

    def get_hls_events(self, source_id: str | None = None,
                       since: float | None = None,
                       until: float | None = None,
                       zone_id=None) -> list[dict]:
        cutoff = time.time() - _PROVISIONAL_GRACE_SECONDS
        live_cutoff = time.time() - _LIVE_HLS_UNREFERENCED_RETENTION_SECONDS
        where, params = [
            "abs_ts>=?",
            "("
            "abs_ts>=?"
            " OR EXISTS ("
            "SELECT 1 FROM segments s"
            " WHERE s.source_id=hls_events.source_id"
            " AND s.end_ts IS NOT NULL"
            " AND s.start_ts<=hls_events.abs_ts"
            " AND s.end_ts>hls_events.abs_ts"
            ")"
            ")",
        ], [cutoff, live_cutoff]
        if source_id and source_id != "all":
            where.append("source_id=?"); params.append(source_id)
        if since is not None:
            where.append("abs_ts>=?"); params.append(since)
        if until is not None:
            where.append("abs_ts<=?"); params.append(until)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM hls_events WHERE {' AND '.join(where)}"
                " ORDER BY abs_ts DESC",
                params,
            ).fetchall()
        events = [{
            "id":          f"h:{r['source_id']}:{r['abs_ts']:.2f}",
            "hls_id":      r["id"],
            "source_id":   r["source_id"],
            "abs_ts":      r["abs_ts"],
            "class":       r["class"],
            "confidence":  r["confidence"],
            "boxes_json":  r["boxes_json"],
            "provisional": True,
            "start_off":   0.0,
            "end_off":     _LIVE_HLS_SEGMENT_SECONDS,
            "segment_id":  None,
        } for r in rows]
        return _filter_with_polygons(events, self.zone_polygons(source_id, zone_id))

    def delete_hls_events(self, source_id: str, since: float, until: float) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM hls_events WHERE source_id=? AND abs_ts>=? AND abs_ts<=?",
                (source_id, since, until),
            )
            return cur.rowcount

    def prune_hls_events(self, max_age_seconds: float = _PROVISIONAL_GRACE_SECONDS) -> None:
        cutoff = time.time() - max_age_seconds
        with self._connect() as conn:
            conn.execute("DELETE FROM hls_events WHERE abs_ts<?", (cutoff,))

    def observe_frame_time(self, source_id: str, abs_ts: float) -> None:
        """Update the open segment's actual_start_ts to MIN(existing, abs_ts).
        Called for each HLS .ts frame seen — earliest abs_ts ≈ MP4 first frame time.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE segments"
                " SET actual_start_ts = MIN(COALESCE(actual_start_ts, ?), ?)"
                " WHERE source_id=? AND end_ts IS NULL"
                "   AND start_ts <= ? + 5 AND start_ts >= ? - 30",
                (abs_ts, abs_ts, source_id, abs_ts, abs_ts),
            )

    def live_status(self, source_id: str | None = None, zone_id=None) -> dict:
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
            latest_rows = conn.execute(
                "SELECT s.source_id, s.start_ts, d.*"
                " FROM segments s JOIN video_detections d ON d.segment_id=s.id"
                f" WHERE {' AND '.join(where)}"
                " ORDER BY (s.start_ts + d.ts_offset) DESC",
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
                "abs_ts": r["start_ts"] + r["ts_offset"],
                "ts_offset": r["ts_offset"],
                "has_human": bool(r["has_human"]),
                "confidence": r["confidence"],
                "boxes": json.loads(r["boxes_json"]) if r["boxes_json"] else [],
                "classes": json.loads(r["classes_json"]) if r["classes_json"] else [],
            }

        # Latest HLS real-time detections (primary source while MP4 is open)
        hls_cutoff = time.time() - 30  # only last 30s of HLS events are "live"
        with self._connect() as conn:
            hls_where = ["abs_ts >= ?"]
            hls_params: list = [hls_cutoff]
            if source_id and source_id != "all":
                hls_where.append("source_id=?"); hls_params.append(source_id)
            hls_rows = conn.execute(
                f"SELECT source_id, abs_ts, class, confidence, boxes_json"
                f" FROM hls_events WHERE {' AND '.join(hls_where)}"
                " ORDER BY abs_ts DESC",
                hls_params,
            ).fetchall()

        # Group by (source_id, abs_ts) — each frame is one detection with all
        # its boxes merged across class rows. Return ALL recent frames so the
        # client can pick the detection matching the displayed video time
        # (HLS player typically buffers 3-6s behind live edge).
        by_frame: dict[tuple, dict] = {}
        for r in hls_rows:
            key = (r["source_id"], round(r["abs_ts"], 2))
            if key not in by_frame:
                by_frame[key] = {
                    "source_id": r["source_id"],
                    "abs_ts": r["abs_ts"],
                    "has_human": False,
                    "confidence": 0.0,
                    "boxes": [],
                    "classes": [],
                }
            det = by_frame[key]
            boxes = json.loads(r["boxes_json"]) if r["boxes_json"] else []
            det["boxes"].extend(boxes)
            det["classes"].append(r["class"])
            det["confidence"] = max(det["confidence"], r["confidence"])
            if r["class"] == "person":
                det["has_human"] = True

        recent = sorted(by_frame.values(), key=lambda d: d["abs_ts"])

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


_CONF_THRESHOLD = 0.35
_CCTV_CLASSES = {
    0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus",
    7: "truck", 14: "bird", 15: "cat", 16: "dog",
    24: "backpack", 28: "suitcase",
}
_CCTV_CLASS_IDS = list(_CCTV_CLASSES.keys())


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


def _yolo_tag_video(model, seg_path: Path, seg_id: int,
                    db: VideoSegmentDB) -> int:
    """Read video file at 1fps, run YOLO, store detections with exact timestamps."""
    import cv2

    cap = cv2.VideoCapture(str(seg_path))
    if not cap.isOpened():
        return 0

    fps        = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step       = max(1, int(round(fps)))  # read every Nth frame = 1fps
    frame_num  = 0
    detections: list[dict] = []

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_num % step == 0:
            ts_ms  = cap.get(cv2.CAP_PROP_POS_MSEC)
            ts_off = ts_ms / 1000.0
            try:
                results = model.predict(frame, classes=_CCTV_CLASS_IDS,
                                        conf=_CONF_THRESHOLD, verbose=False)
                has_human, conf, boxes = _parse_results(results)
                classes = list({b["cls"] for b in boxes}) if boxes else []
                detections.append({
                    "ts_offset": ts_off,
                    "has_human": has_human,
                    "confidence": conf,
                    "boxes": boxes,
                    "classes": classes,
                })
            except Exception:
                LOG.exception("yolo tag failed at %.1fs in %s", ts_off, seg_path.name)
        frame_num += 1

    cap.release()
    db.replace_detections(seg_id, detections)
    return len(detections)


def backfill_events(db: VideoSegmentDB, video_dir: Path | None = None,
                    model=None) -> None:
    """YOLO-tag and extract events for closed segments missing detections."""
    with db._connect() as conn:
        segs = conn.execute(
            "SELECT s.* FROM segments s WHERE s.end_ts IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM video_detections WHERE segment_id=s.id)"
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
    rows = db.track_object_events(segment, tracklets)
    if rows:
        db.insert_object_events(rows)
        db.insert_events(_legacy_events_from_object_events(rows))
    return len(rows)


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

    seg_start = float(segment["start_ts"])
    tracks: list[dict] = []

    for det in detections:
        off = float(det.get("ts_offset", 0.0))
        if off < 0:
            continue
        boxes = [
            b for b in (det.get("boxes") or [])
            if isinstance(b, dict) and b.get("cls")
        ]
        used: set[int] = set()
        for box in sorted(boxes, key=lambda b: float(b.get("conf", 0.0)), reverse=True):
            cls = str(box.get("cls"))
            cx, cy = _box_center(box)
            area = _box_area(box)
            best_idx: int | None = None
            best_dist = _OBJECT_TRACK_CENTER_DISTANCE
            for idx, track in enumerate(tracks):
                if idx in used:
                    continue
                if track["class"] != cls:
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
                    "first": off,
                    "last": off,
                    "cx": cx,
                    "cy": cy,
                    "area": area,
                    "seen": 1,
                    "confidence": float(box.get("conf", 0.0)),
                    "box": dict(box),
                })
                used.add(len(tracks) - 1)
                continue

            track = tracks[best_idx]
            used.add(best_idx)
            track["last"] = off
            track["seen"] += 1
            track["cx"] = (track["cx"] * 0.7) + (cx * 0.3)
            track["cy"] = (track["cy"] * 0.7) + (cy * 0.3)
            track["area"] = (track["area"] * 0.7) + (area * 0.3)
            conf = float(box.get("conf", 0.0))
            if conf >= track["confidence"]:
                track["confidence"] = conf
                track["box"] = dict(box)

    rows: list[dict] = []
    for track in tracks:
        if track["seen"] < _OBJECT_MIN_OBSERVATIONS:
            continue
        box = dict(track["box"])
        box["cls"] = track["class"]
        rows.append({
            "segment_id": segment["id"],
            "source_id": segment["source_id"],
            "abs_ts": seg_start + float(track["first"]),
            "class": track["class"],
            "start_off": float(track["first"]),
            "end_off": float(track["last"]),
            "confidence": float(track["confidence"]),
            "observations": int(track["seen"]),
            "boxes_json": json.dumps([box]),
        })
    rows.sort(key=lambda r: (r["abs_ts"], r["class"]))
    return rows


def _legacy_events_from_object_events(events: list[dict]) -> list[dict]:
    return [
        {
            "segment_id": event["segment_id"],
            "source_id": event["source_id"],
            "abs_ts": event["abs_ts"],
            "class": event["class"],
            "start_off": event["start_off"],
            "end_off": event["end_off"],
            "confidence": event["confidence"],
            "boxes_json": event["boxes_json"],
            "event_type": event["event_type"],
            "track_id": str(event["track_id"]),
        }
        for event in events
        if event.get("segment_id") is not None
    ]


def _public_object_event(event: dict) -> dict:
    row = dict(event)
    row["id"] = f"o:{row['id']}"
    return row


def _event_box(event: dict) -> dict | None:
    try:
        boxes = json.loads(event["boxes_json"]) if event.get("boxes_json") else []
    except (TypeError, json.JSONDecodeError):
        return None
    for box in boxes:
        if isinstance(box, dict):
            return box
    return None


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
    if zone_type not in {"activity_area", "vehicle_event"}:
        raise ValueError("unsupported zone type")
    polygon = _normalize_polygon(zone.get("polygon"))
    if zone_type == "vehicle_event":
        zone_type = "activity_area"
    name = str(zone.get("name") or "Activity area").strip()[:80] or "Activity area"
    return {
        "source_id": source_id,
        "name": name,
        "zone_type": zone_type,
        "polygon_json": json.dumps(polygon, separators=(",", ":")),
        "enabled": 1 if zone.get("enabled", True) else 0,
    }


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
    box = _event_box(event)
    if not box:
        return False
    cx, cy = _box_center(box)
    return _point_in_any_polygon(cx, cy, areas)


def _zone_filter_disabled(zone_id) -> bool:
    return zone_id is not None and str(zone_id).lower() in {"none", "off", "frame", "whole-frame"}


def _filter_with_polygons(events: list[dict], polygons: list[list[dict]]) -> list[dict]:
    if not polygons:
        return list(events)
    return [e for e in events if _event_allowed_by_areas(e, polygons)]


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

    def run(self) -> None:
        """Continuous recording loop — call from a daemon thread."""
        LOG.info("continuous recording started: %s", self.source.id)
        backoff = 5.0
        while not self._stop.is_set():
            try:
                ts = time.time()
                self._start_segment(ts)
                if self._proc:
                    # Poll every 5s so we detect ffmpeg exit within 5 seconds
                    deadline = time.time() + _MAX_SEGMENT_SECONDS
                    while time.time() < deadline and not self._stop.is_set():
                        if self._proc.poll() is not None:
                            LOG.warning("ffmpeg exited early for %s", self.source.id)
                            break
                        self._stop.wait(5)
                    elapsed = time.time() - ts
                    self._stop_segment(time.time())
                    # Reset backoff on successful segment (ran > 30s)
                    backoff = 5.0 if elapsed > 30 else min(backoff * 2, 300)
                    if elapsed <= 30:
                        LOG.warning("short segment (%.0fs) for %s — backoff %.0fs",
                                    elapsed, self.source.id, backoff)
                        self._stop.wait(backoff)
                else:
                    LOG.warning("ffmpeg failed to start for %s — backoff %.0fs",
                                self.source.id, backoff)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 300)
            except Exception:
                LOG.exception("recording error for %s — retry in 30s", self.source.id)
                self._stop.wait(30)
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
                 "-use_wallclock_as_timestamps", "1",
                 "-rtsp_transport", self.source.rtsp_transport,
                 "-i", url,
                 # Archive: MP4 with faststart
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "64k",
                 "-movflags", "+faststart", str(seg_path),
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
            return
        self._seg_path  = seg_path
        self._seg_start = ts
        try:
            self._seg_id = self.db.open_segment(self.source.id, rel_path, ts)
        except Exception:
            self._seg_id = None
        LOG.info("segment started: %s", rel_path)

    def _stop_segment(self, ts: float) -> None:
        proc      = self._proc;     self._proc     = None
        seg_path  = self._seg_path; self._seg_path = None
        seg_id    = self._seg_id;   self._seg_id   = None
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM); proc.wait(timeout=10)
            except Exception:
                proc.kill()
        if seg_id:
            self.db.close_segment(seg_id, ts, None, None)


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
