"""Bounded, cached visual inspection for ambiguous recording searches."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path


PROMPT_VERSION = "camera-observation-v1"
DEFAULT_MODEL = "qwen3-vl:2b-instruct"
DEFAULT_URL = "http://host.docker.internal:11434"

_OBSERVATION_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {
            "type": "string",
            "enum": [
                "fox", "cat", "dog", "person", "vehicle", "bird",
                "other", "unclear",
            ],
        },
        "colours": {"type": "array", "items": {"type": "string"}},
        "action": {"type": "string"},
        "description": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["subject", "colours", "action", "description", "confidence"],
    "additionalProperties": False,
}


class VisualSearchError(RuntimeError):
    pass


class VisualObservationStore:
    """Independent derived-data cache; never contends with the recorder DB."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS visual_observations (
                    event_ref TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    observation_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(event_ref, model, prompt_version)
                );
                CREATE INDEX IF NOT EXISTS vobs_created
                    ON visual_observations(created_at);
            """)

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def get(self, event_ref: str, model: str, prompt_version: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
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
        return (
            {**observation, "created_at": float(row["created_at"])}
            if isinstance(observation, dict) else None
        )

    def put(
        self, event_ref: str, model: str, prompt_version: str, observation: dict
    ) -> None:
        encoded = json.dumps(observation, separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO visual_observations"
                "(event_ref, model, prompt_version, observation_json, created_at)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(event_ref, model, prompt_version) DO UPDATE SET"
                " observation_json=excluded.observation_json,"
                " created_at=excluded.created_at",
                (event_ref, model, prompt_version, encoded, time.time()),
            )


class OllamaVisionClient:
    def __init__(self, *, url: str | None = None, model: str | None = None):
        self.url = (url or os.environ.get("WANYARD_VLM_URL") or DEFAULT_URL).rstrip("/")
        self.model = model or os.environ.get("WANYARD_VLM_MODEL") or DEFAULT_MODEL

    def inspect(self, image: bytes, detector_class: str) -> dict:
        if not image:
            raise VisualSearchError("candidate image is empty")
        prompt = (
            "Inspect this CCTV crop conservatively. The object detector called "
            f"the main subject {detector_class!r}, but it can confuse foxes, cats "
            "and dogs. Describe only what is visibly supported. Use subject "
            "'unclear' when the crop is too poor. Keep colours basic and the "
            "description under 20 words. Return only the requested JSON."
        )
        body = json.dumps({
            "model": self.model,
            "stream": False,
            "format": _OBSERVATION_SCHEMA,
            "messages": [{
                "role": "user",
                "content": prompt,
                "images": [base64.b64encode(image).decode("ascii")],
            }],
            "options": {
                "temperature": 0,
                "num_ctx": 4096,
                "num_predict": 160,
            },
            # Keep the model warm across this search's small candidate batch,
            # then let Ollama release its GPU allocation promptly.
            "keep_alive": "60s",
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.url}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=150) as response:
                payload = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise VisualSearchError(f"visual model unavailable: {exc}") from exc
        try:
            observation = json.loads(payload["message"]["content"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise VisualSearchError("visual model returned invalid JSON") from exc
        return normalize_observation(observation)


def normalize_observation(value: object) -> dict:
    if not isinstance(value, dict):
        raise VisualSearchError("visual observation must be an object")
    subject = str(value.get("subject") or "unclear").strip().casefold()
    allowed = set(_OBSERVATION_SCHEMA["properties"]["subject"]["enum"])
    if subject not in allowed:
        subject = "unclear"
    raw_colours = value.get("colours")
    colours = []
    if isinstance(raw_colours, list):
        colours = list(dict.fromkeys(
            str(item).strip().casefold()[:30]
            for item in raw_colours
            if str(item).strip()
        ))[:6]
    try:
        confidence = max(0.0, min(1.0, float(value.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "subject": subject,
        "colours": colours,
        "action": str(value.get("action") or "").strip()[:120],
        "description": str(value.get("description") or "").strip()[:240],
        "confidence": confidence,
    }


def observation_matches(subject: str, requirements: tuple[str, ...], observation: dict) -> bool:
    observed_subject = str(observation.get("subject") or "unclear").casefold()
    if subject != "anything" and observed_subject != subject:
        return False
    colours = {str(value).casefold() for value in observation.get("colours") or []}
    for requirement in requirements:
        normalized = requirement.casefold()
        if normalized == "species":
            continue
        if normalized == "gray":
            normalized = "grey"
        normalized_colours = {"grey" if colour == "gray" else colour for colour in colours}
        if normalized not in normalized_colours:
            return False
    return float(observation.get("confidence") or 0) >= 0.45
