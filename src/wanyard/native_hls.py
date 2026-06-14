from __future__ import annotations

import os
import urllib.request
from urllib.parse import quote

MANIFEST_NAME = "video1_stream.m3u8"
DEFAULT_PORT = 8888


def hls_port() -> int:
    raw = os.environ.get("WANYARD_MEDIAMTX_HLS_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_PORT
    if 1 <= port <= 65535:
        return port
    return DEFAULT_PORT


def safe_path_part(value: str | None) -> bool:
    return bool(
        value
        and ".." not in value
        and "/" not in value
        and "\\" not in value
    )


def base_url() -> str | None:
    raw = os.environ.get("WANYARD_NATIVE_HLS_BASE_URL", "").strip()
    if not raw:
        relay_host = os.environ.get("WANYARD_RELAY_HOST", "").strip()
        if relay_host:
            raw = f"http://{relay_host}:{hls_port()}"
    return raw.rstrip("/") if raw else None


def source_path(source_id: str) -> str:
    suffix = os.environ.get(
        "WANYARD_NATIVE_HLS_PATH_SUFFIX",
        os.environ.get("WANYARD_RELAY_PATH_SUFFIX", ""),
    ).strip()
    return f"{source_id}{suffix}"


def public_manifest_url(source_id: str) -> str:
    path = source_path(source_id)
    return f"/video/native-live/{quote(path, safe='')}/{MANIFEST_NAME}"


def upstream_url(source_path_value: str, asset: str, query: str = "") -> str | None:
    base = base_url()
    if not base or not safe_path_part(source_path_value) or not safe_path_part(asset):
        return None
    url = f"{base}/{quote(source_path_value, safe='')}/{quote(asset, safe='')}"
    if query:
        url = f"{url}?{query}"
    return url


def timeout_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("WANYARD_NATIVE_HLS_TIMEOUT_SECONDS", "15")))
    except ValueError:
        return 15.0


def media_type(asset: str, upstream_type: str | None) -> str:
    if upstream_type:
        return upstream_type.split(";", 1)[0].strip() or "application/octet-stream"
    if asset.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if asset.endswith(".mp4") or asset.endswith(".m4s"):
        return "video/mp4"
    return "application/octet-stream"


def fetch_asset(
    url: str,
    headers: dict[str, str],
) -> tuple[int, bytes, str | None, dict[str, str]]:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_seconds()) as resp:
        passthrough = {}
        for name in ("Accept-Ranges", "Content-Range"):
            value = resp.headers.get(name)
            if value:
                passthrough[name] = value
        return (
            getattr(resp, "status", 200),
            resp.read(),
            resp.headers.get("Content-Type"),
            passthrough,
        )
