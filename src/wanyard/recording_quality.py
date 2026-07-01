from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_QUALITY_ID = "balanced"
GLOBAL_QUALITY_KEY = "recording_quality_global"
SOURCE_QUALITY_PREFIX = "recording_quality_source:"
STAMPER_RELOAD_FILENAME = "stamper-reload.json"


@dataclass(frozen=True)
class RecordingQualityPreset:
    id: str
    label: str
    summary: str
    cq: int
    crf: int
    hevc_maxrate: str
    hevc_bufsize: str
    h264_maxrate: str
    h264_bufsize: str

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "summary": self.summary,
            "cq": self.cq,
            "crf": self.crf,
            "hevc_maxrate": self.hevc_maxrate,
            "hevc_bufsize": self.hevc_bufsize,
            "h264_maxrate": self.h264_maxrate,
            "h264_bufsize": self.h264_bufsize,
        }


QUALITY_PRESETS: tuple[RecordingQualityPreset, ...] = (
    RecordingQualityPreset(
        "storage_saver",
        "Storage saver",
        "Small archive, lower detail during motion.",
        cq=32,
        crf=28,
        hevc_maxrate="1.5M",
        hevc_bufsize="3M",
        h264_maxrate="2.5M",
        h264_bufsize="5M",
    ),
    RecordingQualityPreset(
        "balanced",
        "Balanced",
        "Current-style retention with good motion detail.",
        cq=28,
        crf=23,
        hevc_maxrate="2.5M",
        hevc_bufsize="5M",
        h264_maxrate="5M",
        h264_bufsize="10M",
    ),
    RecordingQualityPreset(
        "high",
        "High",
        "More motion detail, larger files.",
        cq=24,
        crf=20,
        hevc_maxrate="5M",
        hevc_bufsize="10M",
        h264_maxrate="8M",
        h264_bufsize="16M",
    ),
    RecordingQualityPreset(
        "maximum",
        "Maximum",
        "Near-source preservation, biggest files.",
        cq=20,
        crf=18,
        hevc_maxrate="10M",
        hevc_bufsize="20M",
        h264_maxrate="12M",
        h264_bufsize="24M",
    ),
)

QUALITY_PRESETS_BY_ID = {preset.id: preset for preset in QUALITY_PRESETS}


def source_quality_key(source_id: str) -> str:
    return f"{SOURCE_QUALITY_PREFIX}{source_id}"


def normalize_quality_id(value, default: str | None = None) -> str | None:
    if value is None:
        return default
    quality_id = str(value).strip().lower().replace("-", "_")
    aliases = {
        "saver": "storage_saver",
        "storage": "storage_saver",
        "max": "maximum",
    }
    quality_id = aliases.get(quality_id, quality_id)
    return quality_id if quality_id in QUALITY_PRESETS_BY_ID else default


def validate_quality_id(value) -> str:
    quality_id = normalize_quality_id(value)
    if quality_id is None:
        allowed = ", ".join(preset.id for preset in QUALITY_PRESETS)
        raise ValueError(f"quality must be one of: {allowed}")
    return quality_id


def configured_quality_id(settings, source_id: str) -> str | None:
    override = normalize_quality_id(settings.get_setting(source_quality_key(source_id)))
    if override:
        return override
    return normalize_quality_id(settings.get_setting(GLOBAL_QUALITY_KEY))


def effective_quality_id(settings, source_id: str) -> str:
    return configured_quality_id(settings, source_id) or DEFAULT_QUALITY_ID


def quality_settings_payload(settings, sources: list[dict]) -> dict:
    global_quality = normalize_quality_id(
        settings.get_setting(GLOBAL_QUALITY_KEY), DEFAULT_QUALITY_ID
    )
    overrides: dict[str, str] = {}
    effective: dict[str, str] = {}
    for source in sources:
        source_id = str(source["id"])
        override = normalize_quality_id(settings.get_setting(source_quality_key(source_id)))
        if override:
            overrides[source_id] = override
        effective[source_id] = override or global_quality
    return {
        "presets": [preset.as_dict() for preset in QUALITY_PRESETS],
        "default_quality": DEFAULT_QUALITY_ID,
        "global_quality": global_quality,
        "overrides": overrides,
        "effective": effective,
        "sources": sources,
    }


def encoder_options_for_quality(quality_id: str, codec: str) -> dict[str, str]:
    preset = QUALITY_PRESETS_BY_ID[quality_id]
    if codec.endswith("_nvenc"):
        options = {"cq": str(preset.cq)}
    else:
        options = {"crf": str(preset.crf)}
    if codec == "hevc_nvenc":
        options.update({
            "maxrate": preset.hevc_maxrate,
            "bufsize": preset.hevc_bufsize,
        })
    else:
        options.update({
            "maxrate": preset.h264_maxrate,
            "bufsize": preset.h264_bufsize,
        })
    return options


def stamper_reload_path(source_db_path: str | Path | None) -> Path | None:
    configured = os.environ.get("WANYARD_STAMP_RELOAD_FILE", "").strip()
    if configured:
        return Path(configured)
    if source_db_path is None:
        return None
    return Path(source_db_path).parent / STAMPER_RELOAD_FILENAME


def write_stamper_reload_request(
    source_db_path: str | Path | None,
    source_ids: Iterable[str],
) -> dict | None:
    path = stamper_reload_path(source_db_path)
    if path is None:
        return None
    ids = sorted({str(source_id) for source_id in source_ids if str(source_id)})
    payload = {
        "token": uuid.uuid4().hex,
        "ts": time.time(),
        "source_ids": ids,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{payload['token']}.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return payload


def read_stamper_reload_request(source_db_path: str | Path | None) -> dict | None:
    path = stamper_reload_path(source_db_path)
    if path is None:
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("token"):
        return None
    source_ids = data.get("source_ids")
    if not isinstance(source_ids, list):
        source_ids = []
    return {
        "token": str(data["token"]),
        "ts": data.get("ts"),
        "source_ids": [str(source_id) for source_id in source_ids],
    }
