from __future__ import annotations

import os

from .config import SourceConfig


def resolve_rtsp_url(source: SourceConfig, *, direct: bool = False) -> str | None:
    relay_host = os.environ.get("WANYARD_RELAY_HOST", "").strip()
    if relay_host and not direct:
        return f"rtsp://{relay_host}:8554/{source.id}"
    return source.url
