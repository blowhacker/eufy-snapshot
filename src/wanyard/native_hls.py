from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from urllib.parse import quote

LOG = logging.getLogger("wanyard.native_hls")

# Master (multivariant) playlist, NOT the bare media playlist
# (video1_stream.m3u8). iOS Safari needs the master's #EXT-X-STREAM-INF CODECS
# ="hvc1…" to start the HEVC decoder; a media playlist alone plays H.264 but
# not HEVC on iOS (live showed blank on iPhone after the H.264→HEVC switch).
# The master references the media playlist (relative) → still LL-HLS through
# the same proxy. hls.js (desktop) handles a master playlist fine too.
MANIFEST_NAME = "index.m3u8"
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


WEBRTC_PORT = 8889


def webrtc_base() -> str | None:
    raw = os.environ.get("WANYARD_WEBRTC_BASE_URL", "").strip()
    if not raw:
        relay_host = os.environ.get("WANYARD_RELAY_HOST", "").strip()
        if relay_host:
            raw = f"http://{relay_host}:{WEBRTC_PORT}"
    return raw.rstrip("/") if raw else None


def whep_url(source_path_value: str) -> str | None:
    base = webrtc_base()
    if not base or not safe_path_part(source_path_value):
        return None
    return f"{base}/{quote(source_path_value, safe='')}/whep"


def post_sdp(url: str, body: bytes) -> tuple[int, bytes, str | None]:
    # WHEP: POST the browser's SDP offer to mediamtx, return its SDP answer.
    req = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/sdp"}
    )
    with urllib.request.urlopen(req, timeout=timeout_seconds()) as resp:
        return getattr(resp, "status", 200), resp.read(), resp.headers.get("Content-Type")


def prefers_native_player(user_agent: str | None) -> bool:
    # Mirror the client's shouldUseNativeHls() (video2.js): Safari + iOS play
    # HLS through the OS player (no MSE / hls.js). Everyone else uses hls.js.
    # iOS Chrome/Firefox (CriOS/FxiOS) are WebKit too → caught by the iOS check.
    ua = user_agent or ""
    is_ios = any(tok in ua for tok in ("iPad", "iPhone", "iPod"))
    is_safari = "Safari" in ua and not any(
        tok in ua for tok in ("Chrome", "Chromium", "CriOS", "FxiOS", "Edg", "OPR", "Android")
    )
    return is_ios or is_safari


# LL-HLS tags the native iOS/Safari player can't sustain through the proxy: it
# chases the ~0.6s PART-HOLD-BACK edge, underruns over WiFi, and the blocking
# playlist long-poll starves part fetches behind it on HTTP/1.1 → stalls blank.
# Stripping these turns the media playlist into ordinary live HLS: the OS player
# buffers a few whole segments back from the edge and plays stably (higher
# latency, but it doesn't freeze). hls.js (desktop) still gets the full LL list.
_LL_TAG_PREFIXES = (
    "#EXT-X-PART:",
    "#EXT-X-PART-INF:",
    "#EXT-X-PRELOAD-HINT:",
    "#EXT-X-SERVER-CONTROL:",
    "#EXT-X-RENDITION-REPORT:",
    "#EXT-X-SKIP:",
)


def strip_low_latency(body: bytes) -> bytes:
    if b"#EXT-X-PART" not in body and b"#EXT-X-SERVER-CONTROL" not in body:
        return body  # master playlist or already plain — leave untouched
    out = [
        line for line in body.split(b"\n")
        if not any(line.startswith(p.encode()) for p in _LL_TAG_PREFIXES)
    ]
    return b"\n".join(out)


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
