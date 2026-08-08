from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import SourceConfig

_DDL = """
CREATE TABLE IF NOT EXISTS rtsp_sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    interval_seconds REAL,
    enabled INTEGER NOT NULL DEFAULT 1,
    rtsp_transport TEXT NOT NULL DEFAULT 'tcp',
    timeout_seconds REAL NOT NULL DEFAULT 20,
    output_subdir TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class RtspSourceRow:
    id: str
    name: str
    url: str
    interval_seconds: float | None
    enabled: bool
    rtsp_transport: str
    timeout_seconds: float
    output_subdir: str | None


class SourceDB:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_DDL)

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def _connect(self):
        # Closing context manager — `with sqlite3.connect()` commits but never closes,
        # leaking a connection per call. Close it here.
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list(self) -> list[RtspSourceRow]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM rtsp_sources ORDER BY rowid").fetchall()
        return [_from_row(r) for r in rows]

    def insert(self, row: RtspSourceRow) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO rtsp_sources"
                " (id, name, url, interval_seconds, enabled, rtsp_transport, timeout_seconds, output_subdir)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.id,
                    row.name,
                    row.url,
                    row.interval_seconds,
                    int(row.enabled),
                    row.rtsp_transport,
                    row.timeout_seconds,
                    row.output_subdir,
                ),
            )

    def delete(self, source_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM rtsp_sources WHERE id = ?", (source_id,))
            return cur.rowcount > 0

    def rename(self, source_id: str, name: str) -> bool:
        """Change only the camera's display name; its stable ID is untouched."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE rtsp_sources SET name = ? WHERE id = ?",
                (name, source_id),
            )
            return cur.rowcount > 0

    def ids(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id FROM rtsp_sources").fetchall()
        return {r["id"] for r in rows}

    def get_setting(self, key: str, default=None):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row is not None else default

    def set_setting(self, key: str, value) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_settings(key,value) VALUES(?,?)",
                (key, str(value)),
            )

    def delete_setting(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM app_settings WHERE key=?", (key,))

    def to_source_configs(self) -> tuple[SourceConfig, ...]:
        return tuple(
            SourceConfig(
                id=row.id,
                name=row.name,
                type="rtsp",
                interval_seconds=row.interval_seconds,
                enabled=row.enabled,
                output_subdir=row.output_subdir if row.output_subdir else row.id,
                url=row.url,
                rtsp_transport=row.rtsp_transport,
                timeout_seconds=row.timeout_seconds,
            )
            for row in self.list()
        )


def make_id(name: str, existing: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "rtsp-source"
    candidate = base
    n = 2
    while candidate in existing:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _from_row(row: sqlite3.Row) -> RtspSourceRow:
    return RtspSourceRow(
        id=row["id"],
        name=row["name"],
        url=row["url"],
        interval_seconds=row["interval_seconds"],
        enabled=bool(row["enabled"]),
        rtsp_transport=row["rtsp_transport"],
        timeout_seconds=row["timeout_seconds"],
        output_subdir=row["output_subdir"],
    )
