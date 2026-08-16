"""Per-camera recording mode and footage retention.

Two per-source knobs, each stored where its consumer already reads settings:

- Record mode (``record_mode_source:<id>`` in the SOURCE db): ``continuous``
  (default) runs the normal pipeline; ``live_only`` keeps the camera on the
  realtime wall (raw ingest via go2rtc/mediamtx, untouched by any of this) but
  starts no recorder and no stamper. Because the live detector only follows
  ``-stamped`` relay paths, skipping the stamper also disables detection and
  notifications — live_only is strictly view-only, zero disk and zero GPU.

- Max age (``cleanup_days_source:<id>`` in the VIDEO db, alongside the global
  ``cleanup_days``): per-camera override for the auto-cleanup horizon. Footage
  recorded before a camera went live_only still ages out through this.
"""

from __future__ import annotations

import math
import sqlite3
import shutil
import time
from pathlib import Path

RECORD_MODE_CONTINUOUS = "continuous"
RECORD_MODE_LIVE_ONLY = "live_only"
RECORD_MODES = (RECORD_MODE_CONTINUOUS, RECORD_MODE_LIVE_ONLY)

RECORD_MODE_SOURCE_PREFIX = "record_mode_source:"
CLEANUP_DAYS_SOURCE_PREFIX = "cleanup_days_source:"

_LOCK_RETRY_DELAYS = (0.25, 1.0, 2.0)


def _retry_locked(operation):
    """Retry a bounded maintenance write when another SQLite writer wins.

    VideoSegmentDB already waits ten seconds per connection.  Long detection or
    notification transactions can occasionally exceed that window, and a
    one-shot purge must not strand the camera's files merely because it lost
    that race.
    """
    for attempt in range(len(_LOCK_RETRY_DELAYS) + 1):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            locked = "locked" in str(exc).lower() or "busy" in str(exc).lower()
            if not locked or attempt == len(_LOCK_RETRY_DELAYS):
                raise
            time.sleep(_LOCK_RETRY_DELAYS[attempt])


def record_mode_key(source_id: str) -> str:
    return f"{RECORD_MODE_SOURCE_PREFIX}{source_id}"


def cleanup_days_key(source_id: str) -> str:
    return f"{CLEANUP_DAYS_SOURCE_PREFIX}{source_id}"


def normalize_record_mode(value, default: str | None = None) -> str | None:
    if value is None:
        return default
    mode = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "live": RECORD_MODE_LIVE_ONLY,
        "view_only": RECORD_MODE_LIVE_ONLY,
        "record": RECORD_MODE_CONTINUOUS,
    }
    mode = aliases.get(mode, mode)
    return mode if mode in RECORD_MODES else default


def validate_record_mode(value) -> str:
    mode = normalize_record_mode(value)
    if mode is None:
        allowed = ", ".join(RECORD_MODES)
        raise ValueError(f"record mode must be one of: {allowed}")
    return mode


def record_mode(settings, source_id: str) -> str:
    """Effective record mode for ``source_id`` (source-db settings object)."""
    return normalize_record_mode(
        settings.get_setting(record_mode_key(source_id)), RECORD_MODE_CONTINUOUS
    )


def is_live_only(settings, source_id: str) -> bool:
    return record_mode(settings, source_id) == RECORD_MODE_LIVE_ONLY


def record_modes(settings, source_ids) -> dict[str, str]:
    return {str(sid): record_mode(settings, str(sid)) for sid in source_ids}


def normalize_days(value) -> float | None:
    """Parse a max-age override; None for absent/blank/invalid/non-positive."""
    if value in (None, "", "global"):
        return None
    try:
        days = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(days) or days <= 0:
        return None
    return days


def source_cleanup_days(video_db) -> dict[str, float]:
    """All per-source max-age overrides {source_id: days} from the video db."""
    overrides: dict[str, float] = {}
    for key, value in video_db.get_all_settings().items():
        if not key.startswith(CLEANUP_DAYS_SOURCE_PREFIX):
            continue
        days = normalize_days(value)
        if days is not None:
            overrides[key[len(CLEANUP_DAYS_SOURCE_PREFIX):]] = days
    return overrides


def retention_settings_payload(source_db, video_db, sources: list[dict]) -> dict:
    """Settings-page payload: global thresholds + per-source overrides/modes."""
    global_days = normalize_days(video_db.get_setting("cleanup_days"))
    max_gb = video_db.get_setting("cleanup_max_gb")
    day_overrides = source_cleanup_days(video_db)
    modes: dict[str, str] = {}
    effective_days: dict[str, float | None] = {}
    for source in sources:
        source_id = str(source["id"])
        modes[source_id] = record_mode(source_db, source_id)
        effective_days[source_id] = day_overrides.get(source_id, global_days)
    return {
        "cleanup_days": global_days,
        "cleanup_max_gb": max_gb,
        "day_overrides": day_overrides,
        "record_modes": modes,
        "effective_days": effective_days,
        "sources": sources,
    }


def delete_segments(
    video_db,
    video_dir: Path,
    segments: list[dict],
    notification_cutoffs: dict[str, float] | None = None,
) -> dict[str, int]:
    """Delete selected media and every database object that depends on it.

    Selection policy stays with the caller; this is the single destructive
    executor used by scheduled retention and the settings-page manual action.
    Notification cutoffs are source-scoped because cameras may have different
    retention horizons. A final coverage pass catches holes left by maintenance
    or storage-pressure deletion.
    """
    unique = {int(segment["id"]): segment for segment in segments}
    selected = list(unique.values())
    deleted_files = 0
    freed_bytes = 0

    for segment in selected:
        media = video_dir / segment["path"]
        sidecar = media.with_name(media.name + ".clock.json")
        for path in (media, sidecar):
            try:
                if path.exists():
                    freed_bytes += path.stat().st_size
                    path.unlink()
                    deleted_files += 1
            except OSError:
                pass
        sprite_dir = media.with_suffix("")
        if sprite_dir.is_dir():
            shutil.rmtree(sprite_dir, ignore_errors=True)

    deleted_notifications = 0
    deleted_confirmations = 0
    cutoffs = notification_cutoffs or {}
    def delete_database_rows() -> tuple[int, int]:
        notifications = 0
        confirmations = 0
        with video_db._connect() as conn:
            if selected:
                ids = list(unique)
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"DELETE FROM video_events WHERE segment_id IN ({placeholders})",
                    ids,
                )
                conn.execute(
                    f"DELETE FROM object_events WHERE segment_id IN ({placeholders})",
                    ids,
                )
                conn.execute(
                    f"DELETE FROM video_detections WHERE segment_id IN ({placeholders})",
                    ids,
                )
                conn.execute(
                    f"DELETE FROM segments WHERE id IN ({placeholders})", ids
                )
            for source_id, cutoff in cutoffs.items():
                notifications += conn.execute(
                    "DELETE FROM notification_events"
                    " WHERE source_id = ? AND event_ts < ?",
                    (source_id, cutoff),
                ).rowcount
                confirmations += conn.execute(
                    "DELETE FROM notification_confirmations"
                    " WHERE source_id = ? AND event_ts < ?",
                    (source_id, cutoff),
                ).rowcount
        return notifications, confirmations

    if selected or cutoffs:
        deleted_notifications, deleted_confirmations = _retry_locked(
            delete_database_rows
        )

    orphaned = _retry_locked(video_db.prune_orphan_notifications)
    deleted_notifications += orphaned["events"]
    deleted_confirmations += orphaned["confirmations"]
    return {
        "deleted_segments": len(selected),
        "deleted_files": deleted_files,
        "freed_bytes": freed_bytes,
        "deleted_notifications": deleted_notifications,
        "deleted_confirmations": deleted_confirmations,
    }


def delete_before(
    video_db,
    video_dir: Path,
    cutoff: float,
    source_id: str | None = None,
) -> dict[str, int]:
    """Manual cleanup policy: delete footage and notifications before cutoff."""
    with video_db._connect() as conn:
        where = "end_ts IS NOT NULL AND end_ts < ?"
        params: list = [cutoff]
        if source_id:
            where += " AND source_id = ?"
            params.append(source_id)
        segments = [dict(row) for row in conn.execute(
            "SELECT id, path, end_ts, source_id FROM segments WHERE " + where,
            params,
        ).fetchall()]
        if source_id:
            notification_sources = [source_id]
        else:
            notification_sources = [row[0] for row in conn.execute(
                "SELECT source_id FROM notification_events"
                " UNION SELECT source_id FROM notification_confirmations"
            ).fetchall()]
    return delete_segments(
        video_db,
        video_dir,
        segments,
        {sid: cutoff for sid in notification_sources},
    )


def delete_source_recordings(
    video_db,
    video_dir: Path,
    source_id: str,
) -> dict[str, int]:
    """Permanently remove every recording artifact owned by one source.

    The caller must stop that source's recorder first. Unlike age-based
    retention, this deliberately includes an open segment row so a camera
    removal cannot strand its final clip.
    """
    source_id = str(source_id or "")
    if (
        not source_id
        or source_id in {".", ".."}
        or Path(source_id).name != source_id
    ):
        raise ValueError("invalid source id")

    video_dir = Path(video_dir)
    source_dirs = [video_dir / source_id, video_dir / "live" / source_id]
    resolved_root = video_dir.resolve()
    for directory in source_dirs:
        try:
            directory.resolve().relative_to(resolved_root)
        except ValueError:
            raise ValueError("source directory escapes video root") from None

    bytes_on_disk = 0
    for directory in source_dirs:
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            try:
                if path.is_file():
                    bytes_on_disk += path.stat().st_size
            except OSError:
                pass

    with video_db._connect() as conn:
        segments = [dict(row) for row in conn.execute(
            "SELECT id, path, end_ts, source_id FROM segments"
            " WHERE source_id = ?",
            (source_id,),
        ).fetchall()]

    try:
        result = delete_segments(
            video_db,
            video_dir,
            segments,
            {source_id: float("inf")},
        )
    finally:
        # The user's destructive choice applies to on-disk artifacts even if
        # metadata cleanup loses a prolonged SQLite lock race.  A later
        # retention pass can prune database orphans; leaving the footage would
        # falsely report that "delete recordings" had succeeded while keeping
        # the bytes indefinitely.
        for directory in source_dirs:
            if directory.is_dir():
                shutil.rmtree(directory, ignore_errors=True)

    with video_db._connect() as conn:
        conn.execute(
            "DELETE FROM app_settings WHERE key = ?",
            (cleanup_days_key(source_id),),
        )
    result["freed_bytes"] = max(result["freed_bytes"], bytes_on_disk)
    return result
