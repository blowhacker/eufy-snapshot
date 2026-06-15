from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from urllib.parse import quote

LOG = logging.getLogger("wanyard.native_hls")

MANIFEST_NAME = "video1_stream.m3u8"
DEFAULT_PORT = 8888
CONTROL_API_PORT = 9997


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


# ── mediamtx runtime path registration ───────────────────────────────────
# gen-mediamtx only writes paths at boot, so a camera added at runtime is
# invisible to the relay (the stamper discovers cameras from mediamtx's path
# list, /v3/paths/list). Register the same two paths gen-mediamtx would have
# written, live, via the mediamtx control API — then the stamper picks the
# camera up within its 30s refresh and <id><suffix> appears. A restart still
# regenerates from the DB, so the two stay consistent. Best-effort: failures
# (no relay, mediamtx down) never block the source add.

def _control_api_base() -> str | None:
    host = os.environ.get("WANYARD_RELAY_HOST", "").strip()
    return f"http://{host}:{CONTROL_API_PORT}" if host else None


def _relay_suffix() -> str:
    return os.environ.get("WANYARD_RELAY_PATH_SUFFIX", "").strip()


def _path_api_call(method: str, name: str, body: dict | None = None) -> bool:
    base = _control_api_base()
    if not base:
        return False
    verb = "add" if method == "POST" else "delete"
    url = f"{base}/v3/config/paths/{verb}/{quote(name, safe='')}"
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        return True
    except urllib.error.HTTPError as exc:
        # POST on an existing path → 400; treat as already-registered.
        if method == "POST" and exc.code == 400:
            return True
        LOG.info("mediamtx path %s %s -> HTTP %s", verb, name, exc.code)
        return False
    except Exception as exc:
        LOG.info("mediamtx path %s %s failed: %s", verb, name, exc)
        return False


def register_source_paths(source_id: str, rtsp_url: str, transport: str = "tcp") -> None:
    """Add a camera's ingest + stamped relay paths to mediamtx (live)."""
    _path_api_call("POST", source_id, {
        "source": rtsp_url,
        "sourceOnDemand": True,
        "sourceProtocol": transport if transport in ("tcp", "udp") else "tcp",
    })
    suffix = _relay_suffix()
    if suffix:
        _path_api_call("POST", f"{source_id}{suffix}", {})


def unregister_source_paths(source_id: str) -> None:
    """Remove a camera's relay paths from mediamtx (on source delete)."""
    _path_api_call("DELETE", source_id)
    suffix = _relay_suffix()
    if suffix:
        _path_api_call("DELETE", f"{source_id}{suffix}")
