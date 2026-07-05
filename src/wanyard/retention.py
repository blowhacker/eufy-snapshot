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

RECORD_MODE_CONTINUOUS = "continuous"
RECORD_MODE_LIVE_ONLY = "live_only"
RECORD_MODES = (RECORD_MODE_CONTINUOUS, RECORD_MODE_LIVE_ONLY)

RECORD_MODE_SOURCE_PREFIX = "record_mode_source:"
CLEANUP_DAYS_SOURCE_PREFIX = "cleanup_days_source:"


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
