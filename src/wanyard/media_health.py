from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Callable


_DDL = """
CREATE TABLE IF NOT EXISTS stamper_state (
    source_id        TEXT PRIMARY KEY,
    updated_at       REAL NOT NULL,
    status           TEXT NOT NULL,
    active_encoder   TEXT,
    width            INTEGER,
    height           INTEGER,
    fps              REAL,
    maxrate          TEXT,
    bufsize          TEXT,
    reconnect_count  INTEGER NOT NULL DEFAULT 0,
    fallback_count   INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT
);

CREATE TABLE IF NOT EXISTS media_health_samples (
    id                       INTEGER PRIMARY KEY,
    ts                       REAL NOT NULL,
    source_id                TEXT NOT NULL,
    raw_ready                INTEGER NOT NULL,
    stamped_ready            INTEGER NOT NULL,
    raw_bitrate_bps          REAL,
    stamped_bitrate_bps      REAL,
    hls_age_seconds          REAL,
    recorder_thread_alive    INTEGER NOT NULL,
    recorder_codec           TEXT,
    segment_started_ts       REAL,
    segment_completed_ts     REAL,
    consecutive_failures     INTEGER NOT NULL DEFAULT 0,
    last_failure_kind        TEXT,
    active_encoder           TEXT
);
CREATE INDEX IF NOT EXISTS media_health_samples_source_ts
    ON media_health_samples(source_id, ts);

CREATE TABLE IF NOT EXISTS media_health_events (
    id           INTEGER PRIMARY KEY,
    ts           REAL NOT NULL,
    source_id    TEXT,
    component    TEXT NOT NULL,
    severity     TEXT NOT NULL,
    kind         TEXT NOT NULL,
    message      TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS media_health_events_ts
    ON media_health_events(ts DESC);
CREATE INDEX IF NOT EXISTS media_health_events_source_ts
    ON media_health_events(source_id, ts DESC);
"""

_PATH_METRIC_RE = re.compile(
    r'^paths(?P<bytes>_bytes_received)?\{name="(?P<name>(?:\\.|[^"])*)",'
    r'state="(?P<state>[^"]+)"\}\s+(?P<value>[-+0-9.eE]+)$'
)


def default_db_path(base_dir: Path | None = None) -> Path:
    configured = os.environ.get("WANYARD_MEDIA_HEALTH_DB")
    if configured:
        return Path(configured)
    return (base_dir or Path(".")) / "data" / "media-health.db"


def parse_rate(value: str | None) -> float | None:
    if not value:
        return None
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([kKmMgG]?)\s*", value)
    if not match:
        return None
    multiplier = {"": 1.0, "k": 1e3, "m": 1e6, "g": 1e9}[match.group(2).lower()]
    return float(match.group(1)) * multiplier


def parse_mediamtx_metrics(text: str) -> dict[str, dict]:
    paths: dict[str, dict] = {}
    for line in text.splitlines():
        match = _PATH_METRIC_RE.match(line.strip())
        if not match:
            continue
        name = bytes(match.group("name"), "utf-8").decode("unicode_escape")
        row = paths.setdefault(name, {"ready": False, "bytes_received": None})
        if match.group("bytes"):
            row["bytes_received"] = float(match.group("value"))
        else:
            row["ready"] = (
                match.group("state") == "ready" and float(match.group("value")) > 0
            )
    return paths


class MediaHealthStore:
    def __init__(self, path: Path, *, timeout: float = 1.0) -> None:
        self.path = path
        self.timeout = max(1.0, timeout)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cleanup_lock = threading.Lock()
        self._last_cleanup = 0.0
        with self._connect() as conn:
            conn.executescript(_DDL)
        self.timeout = timeout

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=self.timeout)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={max(1, int(self.timeout * 1000))}")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def event(
        self,
        source_id: str | None,
        component: str,
        severity: str,
        kind: str,
        message: str,
        details: dict | None = None,
        *,
        ts: float | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO media_health_events"
                " (ts,source_id,component,severity,kind,message,details_json)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    ts if ts is not None else time.time(),
                    source_id,
                    component,
                    severity,
                    kind,
                    message,
                    json.dumps(details or {}, separators=(",", ":")),
                ),
            )
        self._maybe_cleanup()

    def update_stamper_state(
        self,
        source_id: str,
        *,
        status: str,
        active_encoder: str | None = None,
        width: int | None = None,
        height: int | None = None,
        fps: float | None = None,
        maxrate: str | None = None,
        bufsize: str | None = None,
        reconnect_count: int = 0,
        fallback_count: int = 0,
        last_error: str | None = None,
        ts: float | None = None,
    ) -> None:
        now = ts if ts is not None else time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO stamper_state"
                " (source_id,updated_at,status,active_encoder,width,height,fps,"
                " maxrate,bufsize,reconnect_count,fallback_count,last_error)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(source_id) DO UPDATE SET"
                " updated_at=excluded.updated_at,status=excluded.status,"
                " active_encoder=COALESCE(excluded.active_encoder,stamper_state.active_encoder),"
                " width=COALESCE(excluded.width,stamper_state.width),"
                " height=COALESCE(excluded.height,stamper_state.height),"
                " fps=COALESCE(excluded.fps,stamper_state.fps),"
                " maxrate=COALESCE(excluded.maxrate,stamper_state.maxrate),"
                " bufsize=COALESCE(excluded.bufsize,stamper_state.bufsize),"
                " reconnect_count=excluded.reconnect_count,"
                " fallback_count=excluded.fallback_count,"
                " last_error=excluded.last_error",
                (
                    source_id,
                    now,
                    status,
                    active_encoder,
                    width,
                    height,
                    fps,
                    maxrate,
                    bufsize,
                    reconnect_count,
                    fallback_count,
                    last_error,
                ),
            )

    def stamper_states(self) -> dict[str, dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM stamper_state").fetchall()
        return {row["source_id"]: dict(row) for row in rows}

    def delete_source(self, source_id: str) -> None:
        """Remove every Media Health reference for one deleted camera."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM media_health_events WHERE source_id=?",
                (source_id,),
            )
            conn.execute(
                "DELETE FROM media_health_samples WHERE source_id=?",
                (source_id,),
            )
            conn.execute(
                "DELETE FROM stamper_state WHERE source_id=?",
                (source_id,),
            )

    def prune_sources(self, active_source_ids: list[str]) -> None:
        """Purge telemetry left by cameras removed before delete cleanup existed."""
        active = sorted(set(active_source_ids))
        with self._connect() as conn:
            if not active:
                conn.execute(
                    "DELETE FROM media_health_events WHERE source_id IS NOT NULL"
                )
                conn.execute("DELETE FROM media_health_samples")
                conn.execute("DELETE FROM stamper_state")
                return
            placeholders = ",".join("?" for _ in active)
            conn.execute(
                "DELETE FROM media_health_events"
                f" WHERE source_id IS NOT NULL AND source_id NOT IN ({placeholders})",
                active,
            )
            conn.execute(
                "DELETE FROM media_health_samples"
                f" WHERE source_id NOT IN ({placeholders})",
                active,
            )
            conn.execute(
                f"DELETE FROM stamper_state WHERE source_id NOT IN ({placeholders})",
                active,
            )

    def record_samples(self, samples: list[dict]) -> None:
        if not samples:
            return
        with self._connect() as conn:
            for sample in samples:
                previous = conn.execute(
                    "SELECT * FROM media_health_samples"
                    " WHERE source_id=? ORDER BY ts DESC LIMIT 1",
                    (sample["source_id"],),
                ).fetchone()
                self._record_transitions(conn, dict(previous) if previous else None, sample)
                conn.execute(
                    "INSERT INTO media_health_samples"
                    " (ts,source_id,raw_ready,stamped_ready,raw_bitrate_bps,"
                    " stamped_bitrate_bps,hls_age_seconds,recorder_thread_alive,"
                    " recorder_codec,segment_started_ts,segment_completed_ts,"
                    " consecutive_failures,last_failure_kind,active_encoder)"
                    " VALUES(:ts,:source_id,:raw_ready,:stamped_ready,:raw_bitrate_bps,"
                    " :stamped_bitrate_bps,:hls_age_seconds,:recorder_thread_alive,"
                    " :recorder_codec,:segment_started_ts,:segment_completed_ts,"
                    " :consecutive_failures,:last_failure_kind,:active_encoder)",
                    sample,
                )
        self._maybe_cleanup()

    def _record_transitions(
        self, conn: sqlite3.Connection, previous: dict | None, current: dict
    ) -> None:
        if previous is None:
            return
        source_id = current["source_id"]
        now = current["ts"]

        def add(severity: str, kind: str, message: str) -> None:
            conn.execute(
                "INSERT INTO media_health_events"
                " (ts,source_id,component,severity,kind,message,details_json)"
                " VALUES(?,?,?,?,?,?,?)",
                (now, source_id, "pipeline", severity, kind, message, "{}"),
            )

        for field, label in (("raw_ready", "camera relay"), ("stamped_ready", "stamped relay")):
            if bool(previous[field]) == bool(current[field]):
                continue
            ready = bool(current[field])
            add(
                "info" if ready else "error",
                f"{field}_{'ready' if ready else 'offline'}",
                f"{label} {'recovered' if ready else 'went offline'}",
            )

        previous_stale = (
            previous["hls_age_seconds"] is None or previous["hls_age_seconds"] > 5
        )
        current_stale = (
            current["hls_age_seconds"] is None or current["hls_age_seconds"] > 5
        )
        if previous_stale != current_stale:
            add(
                "info" if not current_stale else "warning",
                "hls_recovered" if not current_stale else "hls_stale",
                "HLS output recovered" if not current_stale else "HLS output became stale",
            )

        if bool(previous["recorder_thread_alive"]) != bool(current["recorder_thread_alive"]):
            alive = bool(current["recorder_thread_alive"])
            add(
                "info" if alive else "error",
                "recorder_recovered" if alive else "recorder_stopped",
                "recorder thread recovered" if alive else "recorder thread stopped",
            )

        previous_failure = int(previous["consecutive_failures"] or 0)
        current_failure = int(current["consecutive_failures"] or 0)
        if current_failure > previous_failure:
            kind = current.get("last_failure_kind") or "unknown"
            add(
                "error",
                "recorder_failure",
                f"recorder failure: {kind} ({current_failure} consecutive)",
            )
        elif previous_failure > 0 and current_failure == 0:
            add("info", "recorder_failure_cleared", "recorder completed a healthy segment")

        old_encoder = previous.get("active_encoder")
        new_encoder = current.get("active_encoder")
        if old_encoder and new_encoder and old_encoder != new_encoder:
            severity = "warning" if new_encoder == "libx264" else "info"
            add(severity, "encoder_changed", f"encoder changed {old_encoder} -> {new_encoder}")

    def snapshot(self, *, since: float, source_id: str | None = None) -> dict:
        source_filter = " WHERE source_id=?" if source_id else ""
        source_params = (source_id,) if source_id else ()
        with self._connect() as conn:
            current_rows = conn.execute(
                "SELECT s.* FROM media_health_samples s"
                " JOIN (SELECT source_id,MAX(id) id FROM media_health_samples"
                f"{source_filter} GROUP BY source_id) latest ON latest.id=s.id",
                source_params,
            ).fetchall()
            states = {
                row["source_id"]: dict(row)
                for row in conn.execute(
                    f"SELECT * FROM stamper_state{source_filter}", source_params
                ).fetchall()
            }
            event_where = ["ts>=?"]
            event_params: list = [since]
            if source_id:
                event_where.append("source_id=?")
                event_params.append(source_id)
            events = [
                self._event_dict(row)
                for row in conn.execute(
                    "SELECT * FROM media_health_events"
                    f" WHERE {' AND '.join(event_where)}"
                    " ORDER BY ts DESC LIMIT 100",
                    event_params,
                ).fetchall()
            ]
            series = self._series(conn, since, source_id)

        current = {}
        for row in current_rows:
            item = dict(row)
            state = states.get(item["source_id"], {})
            item["stamper"] = state
            item["maxrate_bps"] = parse_rate(state.get("maxrate"))
            current[item["source_id"]] = item
        for sid, state in states.items():
            if sid not in current:
                current[sid] = {
                    "source_id": sid,
                    "ts": None,
                    "stamper": state,
                    "maxrate_bps": parse_rate(state.get("maxrate")),
                }
        return {"current": current, "series": series, "events": events}

    @staticmethod
    def _series(
        conn: sqlite3.Connection, since: float, source_id: str | None
    ) -> list[dict]:
        now = time.time()
        bucket_seconds = max(5, int(math.ceil(max(1.0, now - since) / 720)))
        where = ["ts>=?"]
        params: list = [since]
        if source_id:
            where.append("source_id=?")
            params.append(source_id)
        params = [bucket_seconds, *params]
        rows = conn.execute(
            "SELECT source_id,"
            " CAST(ts / ? AS INTEGER) bucket,"
            " AVG(ts) ts,"
            " AVG(raw_bitrate_bps) raw_bitrate_bps,"
            " AVG(stamped_bitrate_bps) stamped_bitrate_bps,"
            " AVG(hls_age_seconds) hls_age_seconds,"
            " MIN(raw_ready) raw_ready,"
            " MIN(stamped_ready) stamped_ready,"
            " MIN(recorder_thread_alive) recorder_thread_alive,"
            " MAX(consecutive_failures) consecutive_failures"
            " FROM media_health_samples"
            f" WHERE {' AND '.join(where)}"
            " GROUP BY source_id,bucket ORDER BY ts",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict:
        item = dict(row)
        try:
            item["details"] = json.loads(item.pop("details_json"))
        except (TypeError, ValueError):
            item["details"] = {}
            item.pop("details_json", None)
        return item

    def _maybe_cleanup(self) -> None:
        now = time.time()
        if now - self._last_cleanup < 3600:
            return
        with self._cleanup_lock:
            if now - self._last_cleanup < 3600:
                return
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM media_health_samples WHERE ts<?", (now - 14 * 86400,)
                )
                conn.execute(
                    "DELETE FROM media_health_events WHERE ts<?", (now - 30 * 86400,)
                )
            self._last_cleanup = now


class MediaHealthCollector:
    def __init__(
        self,
        store: MediaHealthStore,
        *,
        metrics_url: str | None = None,
        fetch_text: Callable[[str], str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.metrics_url = metrics_url or os.environ.get(
            "WANYARD_MEDIAMTX_METRICS_URL", "http://mediamtx:9998/metrics"
        )
        self.fetch_text = fetch_text or self._fetch_text
        self.clock = clock
        self._previous_counters: dict[str, tuple[float, float]] = {}

    def forget_source(self, source_id: str) -> None:
        self._previous_counters.pop(source_id, None)
        self._previous_counters.pop(f"{source_id}-stamped", None)

    @staticmethod
    def _fetch_text(url: str) -> str:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.read().decode("utf-8", "replace")

    def sample(
        self,
        source_ids: list[str],
        source_statuses: dict[str, dict],
        recorder_statuses: dict[str, dict],
    ) -> list[dict]:
        now = self.clock()
        metrics = parse_mediamtx_metrics(self.fetch_text(self.metrics_url))
        stamper = self.store.stamper_states()
        samples = []
        for source_id in source_ids:
            raw = metrics.get(source_id, {})
            stamped = metrics.get(f"{source_id}-stamped", {})
            recorder = recorder_statuses.get(source_id, {})
            source_status = source_statuses.get(source_id, {})
            samples.append(
                {
                    "ts": now,
                    "source_id": source_id,
                    "raw_ready": int(bool(raw.get("ready"))),
                    "stamped_ready": int(bool(stamped.get("ready"))),
                    "raw_bitrate_bps": self._bitrate(
                        source_id, raw.get("bytes_received"), now
                    ),
                    "stamped_bitrate_bps": self._bitrate(
                        f"{source_id}-stamped", stamped.get("bytes_received"), now
                    ),
                    "hls_age_seconds": source_status.get("hls_age_seconds"),
                    "recorder_thread_alive": int(bool(recorder.get("thread_alive"))),
                    "recorder_codec": recorder.get("codec"),
                    "segment_started_ts": recorder.get("segment_started_ts"),
                    "segment_completed_ts": recorder.get("segment_completed_ts"),
                    "consecutive_failures": int(recorder.get("consecutive_failures") or 0),
                    "last_failure_kind": recorder.get("last_failure_kind"),
                    "active_encoder": stamper.get(source_id, {}).get("active_encoder"),
                }
            )
        self.store.record_samples(samples)
        return samples

    def _bitrate(self, key: str, counter: float | None, now: float) -> float | None:
        if counter is None:
            return None
        previous = self._previous_counters.get(key)
        self._previous_counters[key] = (now, float(counter))
        if previous is None:
            return None
        previous_ts, previous_counter = previous
        elapsed = now - previous_ts
        delta = float(counter) - previous_counter
        if elapsed <= 0 or delta < 0:
            return None
        return delta * 8 / elapsed
