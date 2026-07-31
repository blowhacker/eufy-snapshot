from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, unquote, urlencode

from starlette.background import BackgroundTask
from starlette.applications import Starlette
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import native_hls
from .config import AppConfig
from .detection_settings import (
    configured_detection_classes,
    detection_settings_payload,
    save_detection_classes,
)
from .media_health import MediaHealthCollector, MediaHealthStore, default_db_path
from .ntfy import (
    NtfyPublishError,
    dispatch_ntfy_notifications,
    load_ntfy_config,
    save_ntfy_config,
    send_ntfy_test,
)
from .retention import (
    RECORD_MODE_CONTINUOUS,
    cleanup_days_key,
    delete_before,
    normalize_days as retention_normalize_days,
    record_mode as retention_record_mode,
    record_mode_key,
    retention_settings_payload,
    source_cleanup_days,
    validate_record_mode,
)

LOG = logging.getLogger(__name__)

_THUMB_W  = 160
_IMG_CACHE = "public, max-age=604800, immutable"
_GZIP_SKIP_PREFIXES = (
    "/video/live/",
    "/video/native-live/",
    "/api/thumb",
    "/api/video/event-thumb/",
    "/api/video/live-thumb",
)
# Already-compressed media: gzip gains nothing, burns CPU per request, and a
# gzip+chunked full-file response breaks the browser's byte-range strategy
# for <video> (Content-Length gone, 200 instead of clean 206 semantics) —
# scrubbing needs cheap ranges.
_GZIP_SKIP_SUFFIXES = (".mp4", ".m4s", ".ts", ".jpg", ".jpeg")
_EVENT_THUMB_MAX_W = 640
# Encounters can now represent a complete walk rather than a short detector
# burst.  Let previews follow that span, but do not let a static false positive
# monopolise the feed for an entire recording segment.
_DETECTION_PREVIEW_MAX_SECONDS = 90.0


def _gzip_path_is_excluded(
    path: str,
    prefixes: tuple[str, ...],
    suffixes: tuple[str, ...],
) -> bool:
    return (
        any(path.startswith(prefix) for prefix in prefixes)
        or path.endswith(suffixes)
        or (
            path.startswith("/api/notifications/")
            and path.endswith("/thumb")
        )
    )


class _PathAwareGZipMiddleware:
    def __init__(self, app, *, minimum_size: int, skip_prefixes: tuple[str, ...],
                 skip_suffixes: tuple[str, ...] = ()):
        self.app = app
        self.gzip_app = GZipMiddleware(app, minimum_size=minimum_size)
        self.skip_prefixes = skip_prefixes
        self.skip_suffixes = skip_suffixes

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "").lower()
            if _gzip_path_is_excluded(
                path, self.skip_prefixes, self.skip_suffixes
            ):
                await self.app(scope, receive, send)
                return
        await self.gzip_app(scope, receive, send)


def _read_live_hls_window(video_dir: Path | None, source_id: str | None) -> dict | None:
    if not video_dir or not source_id or ".." in source_id:
        return None
    from wanyard import media_time
    return media_time.live_window(video_dir, source_id)


def _optional_float_query(request: Request, name: str) -> float | None:
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _detection_wall_camera(
    video_db,
    source: dict,
    classes: list[str],
    limit: int,
    before: float | None,
    polygons: list[list[dict]] | None = None,
    zone_filter_active: bool = False,
    live_window: dict | None = None,
    exclusions: list[list[dict]] | None = None,
) -> dict:
    """Return one camera's newest detection episodes for the thumbnail wall.

    Keep pagination source-local: a quiet camera must not drag a busy camera's
    cursor backwards and make the busy camera skip events.
    """
    fetch_limit = limit + 1
    exclusions = exclusions or []
    if zone_filter_active and not polygons:
        recorded = []
    elif zone_filter_active or exclusions:
        recorded = _detection_wall_filtered_recorded(
            video_db,
            source["id"],
            classes,
            fetch_limit,
            before,
            polygons or [],
            exclusions,
        )
    else:
        recorded = video_db.list_detection_events(
            source["id"], classes, fetch_limit, before
        )

    provisional_getter = getattr(
        video_db,
        "provisional_detection_events",
        video_db.provisional_events,
    )
    provisional = provisional_getter(source["id"])
    if zone_filter_active or exclusions:
        from .video import _filter_with_zone_policy
        provisional = (
            _filter_with_zone_policy(provisional, polygons or [], exclusions)
            if (polygons or exclusions) else []
        )
    wanted = set(classes)
    candidates: dict[str, dict] = {}
    for event in [*provisional, *recorded]:
        event_ts = event.get("display_ts", event.get("abs_ts"))
        try:
            event_ts = float(event_ts)
        except (TypeError, ValueError):
            continue
        if before is not None and event_ts >= before:
            continue
        if wanted and event.get("class") not in wanted:
            continue
        # Object permanence emits a matching "disappeared" row. The wall is a
        # wall of detections, so show the appearance/legacy detection episode.
        if event.get("event_type") == "disappeared":
            continue
        event_id = str(event.get("id", ""))
        if not event_id:
            continue
        candidates[event_id] = event

    events = sorted(
        candidates.values(),
        key=lambda event: (
            float(event.get("display_ts", event.get("abs_ts", 0))),
            str(event.get("id", "")),
        ),
        reverse=True,
    )
    has_more = len(events) > limit
    events = events[:limit]

    public_events = []
    for event in events:
        event_id = str(event["id"])
        event_ts = float(event.get("display_ts", event["abs_ts"]))
        cls = str(event.get("class") or "motion")
        preview = _detection_wall_preview(event, cls, live_window)
        public_events.append({
            "id": event_id,
            "source_id": source["id"],
            "source_name": source.get("name") or source["id"],
            "abs_ts": float(event["abs_ts"]),
            "display_ts": event_ts,
            "class": cls,
            "start_off": float(event.get("start_off") or 0),
            "end_off": float(event.get("end_off") or 0),
            "confidence": float(event.get("confidence") or 0),
            "provisional": bool(event.get("provisional")),
            "thumb_url": f"/api/video/event-thumb/{quote(event_id, safe='')}",
            "target_url": (
                f"/?{urlencode({'source': source['id'], 'ts': f'{event_ts:.3f}', 'cls': cls, 'zone': 'none'})}"
            ),
            "preview": preview,
        })

    return {
        "id": source["id"],
        "name": source.get("name") or source["id"],
        "record_mode": source.get("record_mode", RECORD_MODE_CONTINUOUS),
        "events": public_events,
        # The next request is exclusive of this time. Event episode timestamps
        # are frame-clock values, so subtracting a microsecond preserves the
        # immediately preceding frame without returning the last row again.
        "next_before": (
            public_events[-1]["display_ts"] - 0.000001
            if has_more and public_events else None
        ),
    }


def _detection_wall_filtered_recorded(
    video_db,
    source_id: str,
    classes: list[str],
    limit: int,
    before: float | None,
    polygons: list[list[dict]],
    exclusions: list[list[dict]] | None = None,
) -> list[dict]:
    """Scan indexed time pages until ``limit`` zone-matching rows are found."""
    from .video import _filter_with_zone_policy

    matched: list[dict] = []
    cursor = before
    batch_limit = max(200, limit * 4)
    while len(matched) < limit:
        page = video_db.list_detection_events(
            source_id, classes, batch_limit, cursor
        )
        if not page:
            break
        matched.extend(_filter_with_zone_policy(page, polygons, exclusions or []))
        if len(page) < batch_limit:
            break
        oldest = min(
            float(event.get("display_ts", event.get("abs_ts", 0)))
            for event in page
        )
        next_cursor = oldest - 0.000001
        if cursor is not None and next_cursor >= cursor:
            break
        cursor = next_cursor
    return matched[:limit]


def _detection_wall_preview(
    event: dict,
    cls: str,
    live_window: dict | None = None,
) -> dict | None:
    """Preview metadata for a recorded MP4 or provisional live-HLS event."""
    try:
        boxes = json.loads(event["boxes_json"]) if event.get("boxes_json") else []
    except (TypeError, json.JSONDecodeError):
        return None
    box = _select_event_box(boxes, cls)
    if not box:
        return None
    try:
        coordinates = {
            key: float(box[key])
            for key in ("x1", "y1", "x2", "y2")
        }
        raw_start = float(event.get("start_off") or 0)
        raw_end = float(event.get("end_off") or raw_start)
    except (KeyError, TypeError, ValueError):
        return None
    numeric_values = (*coordinates.values(), raw_start, raw_end)
    if not all(math.isfinite(value) for value in numeric_values):
        return None
    clean_box = {
        key: max(0.0, min(1.0, value))
        for key, value in coordinates.items()
    }
    start_off = max(0.0, raw_start)
    end_off = max(start_off, raw_end)
    if (
        clean_box["x2"] <= clean_box["x1"]
        or clean_box["y2"] <= clean_box["y1"]
    ):
        return None

    if event.get("provisional"):
        if not live_window:
            return None
        source_id = str(event.get("source_id") or "")
        if not source_id:
            return None
        try:
            event_ts = float(
                event.get("display_ts", event.get("abs_ts"))
            )
            window_start = float(live_window["start_ts"])
            window_end = float(live_window["end_ts"])
        except (KeyError, TypeError, ValueError):
            return None
        if not all(math.isfinite(value) for value in (
            event_ts, window_start, window_end
        )):
            return None
        if (
            window_end <= window_start
            or event_ts < window_start - 1.0
            or event_ts > window_end + 1.0
        ):
            return None
        event_ts = max(window_start, min(window_end, event_ts))
        duration = max(0.0, end_off - start_off)
        start_ts = max(window_start, event_ts - 1.0)
        end_ts = min(
            window_end,
            max(start_ts + 3.0, event_ts + duration + 1.0),
        )
        end_ts = min(
            end_ts,
            start_ts + _DETECTION_PREVIEW_MAX_SECONDS,
        )
        if end_ts - start_ts < 0.25:
            return None
        return {
            "kind": "hls",
            "url": (
                f"/video/live/{quote(source_id, safe='')}/live.m3u8"
            ),
            "source_id": source_id,
            "class": cls,
            "event_ts": round(event_ts, 3),
            "start_ts": round(start_ts, 3),
            "end_ts": round(end_ts, 3),
            "box": clean_box,
        }

    seg_path = str(event.get("seg_path") or "")
    source_id = str(event.get("source_id") or "")
    if not seg_path or not seg_path.lower().endswith(".mp4"):
        return None
    try:
        event_ts = float(event.get("abs_ts"))
    except (TypeError, ValueError):
        return None
    if not source_id or not math.isfinite(event_ts):
        return None
    start = max(0.0, start_off - 1.0)
    end = min(
        start + _DETECTION_PREVIEW_MAX_SECONDS,
        max(start + 3.0, end_off + 1.0),
    )
    return {
        "url": f"/video/files/{quote(seg_path, safe='/')}",
        "source_id": source_id,
        "class": cls,
        "event_ts": round(event_ts, 3),
        "start_ts": round(event_ts - (start_off - start), 3),
        "end_ts": round(event_ts + (end - start_off), 3),
        "start": round(start, 3),
        "end": round(end, 3),
        "box": clean_box,
    }


def _detection_wall_all(
    video_db,
    sources: list[dict],
    classes: list[str],
    limit: int,
    before: float | None,
    polygons_by_source: dict[str, list[list[dict]]] | None = None,
    live_windows_by_source: dict[str, dict] | None = None,
    exclusions_by_source: dict[str, list[list[dict]]] | None = None,
) -> dict:
    """Interleave source-local pages into one newest-first camera feed."""
    source_pages = [
        _detection_wall_camera(
            video_db,
            source,
            classes,
            limit + 1,
            before,
            (polygons_by_source or {}).get(source["id"]),
            source["id"] in (polygons_by_source or {}),
            (live_windows_by_source or {}).get(source["id"]),
            (exclusions_by_source or {}).get(source["id"]),
        )
        for source in sources
    ]
    events = [
        event
        for page in source_pages
        for event in page["events"]
    ]
    events.sort(
        key=lambda event: (
            float(event["display_ts"]),
            event["source_id"],
            str(event["id"]),
        ),
        reverse=True,
    )
    has_more = (
        len(events) > limit
        or any(page["next_before"] is not None for page in source_pages)
    )
    events = events[:limit]
    return {
        "id": "all",
        "name": "All cameras",
        "record_mode": RECORD_MODE_CONTINUOUS,
        "events": events,
        "next_before": (
            events[-1]["display_ts"] - 0.000001
            if has_more and events else None
        ),
    }


# Overlay association — kept in lockstep with the browser overlay
# (video2.js overlayTracklets). Chain per-frame boxes into tracklets so a burned
# clip box only interpolates toward the SAME object: mutual-NN + constant-velocity
# gate + heading veto. Same constants as the JS so clip == on-screen overlay.
_OVL_MAX_GAP    = 2.5
_OVL_SNAP       = 0.8
_OVL_GATE_FLOOR = 0.22
_OVL_GATE_K     = 2.5
_OVL_WARM_GATE  = 0.40
_OVL_MIN_SPEED  = 0.04


def _hypot(a: float, b: float) -> float:
    return (a * a + b * b) ** 0.5


def _overlay_tracklets(samples: list) -> list:
    """samples: [(rel_ts, [box {cls,conf,x1,y1,x2,y2,cx,cy}])] sorted by rel_ts.
    Returns [{cls, pts:[{t, box, cx, cy}]}] using the same association as the UI."""
    tracks: list = []
    for rel, boxes in samples:
        heads = [t for t in tracks if rel - t["pts"][-1]["t"] <= _OVL_MAX_GAP]
        cands = [{"box": b, "cx": b["cx"], "cy": b["cy"], "cls": b["cls"],
                  "used": False, "bestHead": None} for b in boxes]
        preds = []
        for t in heads:
            h = t["pts"][-1]; dt = rel - h["t"]; mv = t["vx"] is not None
            px = h["cx"] + t["vx"] * dt if mv else h["cx"]
            py = h["cy"] + t["vy"] * dt if mv else h["cy"]
            gate = (max(_OVL_GATE_FLOOR, _OVL_GATE_K * _hypot(t["vx"], t["vy"]) * dt)
                    if mv else _OVL_WARM_GATE)
            preds.append({"t": t, "h": h, "dt": dt, "px": px, "py": py,
                          "gate": gate, "best": None, "bestDist": float("inf")})
        for p in preds:
            for c in cands:
                if c["cls"] != p["t"]["cls"]:
                    continue
                d = _hypot(c["cx"] - p["px"], c["cy"] - p["py"])
                if d < p["bestDist"]:
                    p["bestDist"] = d; p["best"] = c
        for c in cands:
            bd = float("inf")
            for p in preds:
                if p["t"]["cls"] != c["cls"]:
                    continue
                d = _hypot(c["cx"] - p["h"]["cx"], c["cy"] - p["h"]["cy"])
                if d < bd:
                    bd = d; c["bestHead"] = p
        for p in preds:
            b = p["best"]
            if not b or b["used"] or b["bestHead"] is not p or p["bestDist"] > p["gate"]:
                continue
            t = p["t"]
            if t["vx"] is not None and _hypot(t["vx"], t["vy"]) > _OVL_MIN_SPEED:
                if t["vx"] * (b["cx"] - p["h"]["cx"]) + t["vy"] * (b["cy"] - p["h"]["cy"]) < 0:
                    continue   # heading reversal -> reject
            t["vx"] = (b["cx"] - p["h"]["cx"]) / p["dt"]
            t["vy"] = (b["cy"] - p["h"]["cy"]) / p["dt"]
            t["pts"].append({"t": rel, "box": b["box"], "cx": b["cx"], "cy": b["cy"]})
            b["used"] = True
        for c in cands:
            if not c["used"]:
                tracks.append({"cls": c["cls"], "vx": None, "vy": None,
                               "pts": [{"t": rel, "box": c["box"], "cx": c["cx"], "cy": c["cy"]}]})
    return tracks


def _generate_thumb(src: Path, dest: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(src), "-vf", f"scale={_THUMB_W}:-2",
             "-frames:v", "1", "-q:v", "6", str(dest)],
            capture_output=True, timeout=15, check=False,
        )
        return r.returncode == 0 and dest.exists()
    except (subprocess.TimeoutExpired, OSError):
        return False


def _probe_video_size(path: Path) -> tuple[int, int] | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
             str(path)],
            capture_output=True, timeout=5, check=False, text=True,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    raw = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    try:
        w, h = (int(x) for x in raw.split("x", 1))
    except ValueError:
        return None
    return (w, h) if w > 0 and h > 0 else None


def _select_event_box(boxes: list, cls: str) -> dict | None:
    candidates = [b for b in boxes if isinstance(b, dict)]
    if not candidates:
        return None
    display_candidates = [
        box for box in candidates
        if not box.get("_zone_sample")
    ] or candidates
    matching = [
        b for b in display_candidates if b.get("cls") == cls
    ] or display_candidates

    def score(box: dict) -> tuple[float, float]:
        try:
            area = max(0.0, float(box["x2"]) - float(box["x1"])) * \
                   max(0.0, float(box["y2"]) - float(box["y1"]))
        except (KeyError, TypeError, ValueError):
            area = 0.0
        try:
            conf = float(box.get("conf", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        return conf, area

    return max(matching, key=score)


def _crop_from_box(box: dict, frame_w: int, frame_h: int,
                   aspect: float = 4 / 3) -> tuple[int, int, int, int] | None:
    try:
        x1 = max(0.0, min(1.0, float(box["x1"]))) * frame_w
        y1 = max(0.0, min(1.0, float(box["y1"]))) * frame_h
        x2 = max(0.0, min(1.0, float(box["x2"]))) * frame_w
        y2 = max(0.0, min(1.0, float(box["y2"]))) * frame_h
    except (KeyError, TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None

    bw, bh = x2 - x1, y2 - y1
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    pad = max(24.0, max(bw, bh) * 0.45)
    cw, ch = bw + pad * 2, bh + pad * 2
    if cw / ch < aspect:
        cw = ch * aspect
    else:
        ch = cw / aspect
    cw = min(float(frame_w), max(96.0, cw))
    ch = min(float(frame_h), max(72.0, ch))
    if cw / ch < aspect:
        cw = min(float(frame_w), ch * aspect)
    else:
        ch = min(float(frame_h), cw / aspect)

    rw = min(frame_w, max(2, round(cw)))
    rh = min(frame_h, max(2, round(ch)))
    x = max(0.0, min(float(frame_w - rw), cx - rw / 2))
    y = max(0.0, min(float(frame_h - rh), cy - rh / 2))
    return round(x), round(y), rw, rh


def _extract_video_thumb(seg_path: Path, cache_file: Path, t: float,
                         crop_box: dict | None = None) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False

    vf = None
    if crop_box:
        size = _probe_video_size(seg_path)
        crop = _crop_from_box(crop_box, *size) if size else None
        if crop:
            x, y, w, h = crop
            vf = (
                f"crop={w}:{h}:{x}:{y},"
                f"scale='min({_EVENT_THUMB_MAX_W},iw)':-2"
            )

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    for attempt_t in [t, max(0, t - 1), max(0, t - 2), max(0, t - 5)]:
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(attempt_t), "-i", str(seg_path),
        ]
        if vf:
            cmd += ["-vf", vf]
        cmd += ["-frames:v", "1", "-q:v", "3", str(cache_file)]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=10, check=False)
        except (subprocess.TimeoutExpired, OSError):
            r = None
        if r and r.returncode == 0 and cache_file.exists() and cache_file.stat().st_size > 0:
            return True
    try:
        cache_file.unlink(missing_ok=True)
    except OSError:
        pass
    return False


# ── Live-thumb (provisional/open-segment event) cache ─────────────────────
_LIVE_THUMB_CACHE: dict[tuple, tuple[float, bytes | None]] = {}
_LIVE_THUMB_CACHE_TTL = 30.0
_LIVE_THUMB_CACHE_MAX = 256


def _live_thumb_cache_get(key: tuple) -> bytes | None | object:
    entry = _LIVE_THUMB_CACHE.get(key)
    if not entry:
        return None
    ts, data = entry
    if time.time() - ts > _LIVE_THUMB_CACHE_TTL:
        _LIVE_THUMB_CACHE.pop(key, None)
        return None
    return data if data else b""  # b"" is a cached "not found"


def _live_thumb_cache_put(key: tuple, data: bytes | None) -> None:
    if len(_LIVE_THUMB_CACHE) > _LIVE_THUMB_CACHE_MAX:
        _LIVE_THUMB_CACHE.clear()
    _LIVE_THUMB_CACHE[key] = (time.time(), data)


def _extract_live_thumb(video_dir: Path, source_id: str, ts: float,
                        box: dict, max_drift: float = 0.5) -> bytes | None:
    """Find the live HLS fragment frame whose clock is closest to ``ts`` and
    crop ``box`` from it. Returns JPEG bytes or None.

    The frame clock is read from SEI side data. Every returned frame is
    verified against its own clock, so a stray decode outside ``max_drift``
    can never produce a wrong-moment thumb.
    """
    import av

    from . import media_time, sei
    from .yolo_server import _crop_thumb

    live_dir = (video_dir / "live" / source_id).resolve()
    try:
        live_dir.relative_to(video_dir.resolve())
    except ValueError:
        return None
    if not live_dir.is_dir():
        return None

    window = media_time.live_window(video_dir, source_id)
    candidates: list[Path] = []
    if window:
        for segment in window.get("segments", []):
            if segment["start_ts"] - 0.5 <= ts <= segment["end_ts"] + 0.5:
                candidates.append(live_dir / segment["uri"])
    if not candidates:
        # Fallback for temporarily undecodable playlists. Fragment mtimes are
        # only an operational hint; every returned frame is still verified
        # against its own clock below.
        all_frags = sorted(live_dir.glob("seg_*.ts"), key=lambda p: p.stat().st_mtime, reverse=True)
        candidates = [
            p for p in all_frags
            if ts - 1.0 <= p.stat().st_mtime <= ts + 8.0
        ]
    if not candidates:
        return None

    best_frame = None       # bgr ndarray
    best_diff = max_drift
    for p in candidates[:4]:
        try:
            container = av.open(str(p))
        except Exception:
            continue
        try:
            for frame in container.decode(video=0):
                marker, crc_ok = sei.decode_frame(frame)
                if not crc_ok or marker is None:
                    continue
                diff = abs(marker - ts)
                if diff < best_diff:
                    best_diff = diff
                    best_frame = frame.to_ndarray(format="bgr24")
                    if diff < 0.02:
                        break
        except Exception:
            LOG.debug("live thumb decode failed for %s", p, exc_info=True)
        finally:
            try:
                container.close()
            except Exception:
                pass
        if best_frame is not None and best_diff < 0.02:
            break

    if best_frame is None:
        return None

    cls = str(box.get("cls") or "")
    return _crop_thumb(best_frame, [box], cls)


def make_app(
    config: AppConfig,
    source_db=None,
    video_dir=None,
    video_db=None,
    capture_worker=None,
) -> Starlette:
    import wanyard
    static_dir = Path(wanyard.__file__).parent / "static"
    health_store = None
    health_collector = None
    try:
        base_dir = Path(video_dir).parent if video_dir else Path(".")
        health_store = MediaHealthStore(default_db_path(base_dir))
        health_collector = MediaHealthCollector(health_store)
    except Exception:
        LOG.warning("media health database unavailable", exc_info=True)

    async def _notification_materialize_loop(interval: float):
        # Generate notifications in the backend, independent of any browser.
        # Detections become notifications within `interval` of being recorded,
        # whether or not the UI is open. The work is cheap when idle (a few
        # SQLite reads) and only calls YOLO for genuinely new detections.
        while True:
            try:
                await asyncio.to_thread(video_db.materialize_notifications)
                await asyncio.to_thread(dispatch_ntfy_notifications, video_db)
            except Exception:
                LOG.exception("notification materialize loop error")
            await asyncio.sleep(interval)

    async def _media_health_loop(interval: float):
        while True:
            try:
                sources = _sources_list(config, source_db)
                source_ids = [source["id"] for source in sources]
                await asyncio.to_thread(
                    health_store.prune_sources, source_ids
                )
                source_statuses = await asyncio.to_thread(_source_statuses)
                recorder_statuses = (
                    capture_worker.recorder_status() if capture_worker else {}
                )
                await asyncio.to_thread(
                    health_collector.sample,
                    source_ids,
                    source_statuses,
                    recorder_statuses,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("media health sample failed")
            await asyncio.sleep(interval)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        if capture_worker:
            capture_worker.start()
        notify_task = None
        health_task = None
        if video_db is not None:
            try:
                interval = float(os.environ.get("NOTIFICATION_POLL_INTERVAL", "5"))
            except ValueError:
                interval = 5.0
            interval = max(1.0, interval)
            notify_task = asyncio.create_task(_notification_materialize_loop(interval))
        if health_collector is not None:
            try:
                health_interval = float(
                    os.environ.get("WANYARD_MEDIA_HEALTH_INTERVAL", "15")
                )
            except ValueError:
                health_interval = 15.0
            health_task = asyncio.create_task(
                _media_health_loop(max(5.0, health_interval))
            )
        try:
            yield
        finally:
            for task in (notify_task, health_task):
                if task is None:
                    continue
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            if capture_worker:
                await asyncio.to_thread(capture_worker.stop)

    # ── API handlers ──────────────────────────────────────

    async def api_health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def api_sources(request: Request) -> JSONResponse:
        if request.method == "GET":
            return JSONResponse({"sources": _sources_list(config, source_db)})

        if source_db is None:
            return JSONResponse({"error": "db_path not configured"}, status_code=501)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)

        name = str(body.get("name", "")).strip()
        url  = str(body.get("url",  "")).strip()
        if not name:
            return JSONResponse({"error": "name is required"}, status_code=400)
        if not url or not (url.startswith("rtsp://") or url.startswith("rtsps://")):
            return JSONResponse({"error": "url must start with rtsp:// or rtsps://"}, status_code=400)

        from .db import RtspSourceRow, make_id
        existing_ids = source_db.ids()
        source_id = make_id(name, existing_ids)
        interval_raw = body.get("interval_seconds")
        transport = str(body.get("rtsp_transport", "tcp"))
        if transport not in {"tcp", "udp"}:
            transport = "tcp"

        row = RtspSourceRow(
            id=source_id, name=name, url=url,
            interval_seconds=float(interval_raw) if interval_raw is not None else None,
            enabled=True, rtsp_transport=transport,
            timeout_seconds=float(body.get("timeout_seconds", 20)),
            output_subdir=source_id,
        )
        source_db.insert(row)
        # Register go2rtc ingest/WebRTC and relay paths live. Both static configs
        # are generated at boot, but adding a camera must not require a restart.
        await asyncio.to_thread(
            native_hls.register_source_runtime, source_id, url, transport)
        updated = source_db.to_source_configs()
        new = next(s for s in updated if s.id == source_id)
        return JSONResponse({
            "source": {
                "id": new.id, "name": new.name, "type": "rtsp",
                "enabled": True,
                "interval_seconds": new.interval(config.interval_seconds),
                "mutable": True,
            }
        }, status_code=201)

    async def api_detection_wall(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({
                "classes": {},
                "available_classes": [],
                "counted_classes": [],
                "class_counts_complete": False,
                "cameras": [],
                "zones": [],
                "selected_zones": [],
                "sources": [
                    {
                        "id": source["id"],
                        "name": source.get("name") or source["id"],
                        "record_mode": source.get(
                            "record_mode", RECORD_MODE_CONTINUOUS
                        ),
                    }
                    for source in _sources_list(config, source_db)
                ],
                "generated_at": time.time(),
            })

        all_sources = _sources_list(config, source_db)
        source_id = request.query_params.get("source") or "all"
        selected_sources = all_sources
        if source_id != "all":
            selected_sources = [
                source for source in all_sources if source["id"] == source_id
            ]
            if not selected_sources:
                return JSONResponse({"error": "source not found"}, status_code=404)

        raw_classes = request.query_params.get("classes") or ""
        classes = list(dict.fromkeys(
            cls.strip()
            for cls in raw_classes.split(",")
            if cls.strip()
        ))
        if len(classes) > 80 or any(len(cls) > 80 for cls in classes):
            return JSONResponse({"error": "invalid classes"}, status_code=400)

        raw_zones = request.query_params.get("zones") or ""
        requested_zone_uids = list(dict.fromkeys(
            uid.strip()
            for uid in raw_zones.split(",")
            if uid.strip()
        ))
        if (
            len(requested_zone_uids) > 64
            or any(
                len(uid) > 80
                or not all(ch.isalnum() or ch in {"-", "_"} for ch in uid)
                for uid in requested_zone_uids
            )
        ):
            return JSONResponse({"error": "invalid zones"}, status_code=400)

        try:
            limit = int(request.query_params.get("limit", "24"))
            before = _optional_float_query(request, "before")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        limit = min(60, max(8, limit))
        include_counts = (
            request.query_params.get("counts", "1").strip().lower()
            not in {"0", "false", "no", "off"}
        )
        include_events = (
            request.query_params.get("events", "1").strip().lower()
            not in {"0", "false", "no", "off"}
        )

        def _payload() -> dict:
            available_classes = configured_detection_classes(video_db)
            all_source_ids = {
                source["id"] for source in all_sources
            }
            source_names = {
                source["id"]: source.get("name") or source["id"]
                for source in all_sources
            }
            all_zone_rows = [
                zone
                for zone in video_db.list_zones()
                if (
                    zone.get("enabled", True)
                    and zone.get("uid")
                    and zone.get("source_id") in all_source_ids
                    and isinstance(zone.get("polygon"), list)
                    and len(zone["polygon"]) >= 3
                )
            ]
            available_zone_rows = [
                zone for zone in all_zone_rows
                if zone.get("type", "activity_area") in {"activity_area", "vehicle_event"}
            ]
            exclusion_rows = [
                zone for zone in all_zone_rows
                if zone.get("type") == "exclusion_area"
            ]
            zones_by_uid = {
                str(zone["uid"]): zone for zone in available_zone_rows
            }
            selected_zone_uids = [
                uid for uid in requested_zone_uids if uid in zones_by_uid
            ]
            polygons_by_source: dict[str, list[list[dict]]] = {}
            for uid in selected_zone_uids:
                zone = zones_by_uid[uid]
                polygons_by_source.setdefault(
                    str(zone["source_id"]), []
                ).append(zone["polygon"])
            exclusions_by_source: dict[str, list[list[dict]]] = {}
            for zone in exclusion_rows:
                exclusions_by_source.setdefault(
                    str(zone["source_id"]), []
                ).append(zone["polygon"])

            live_windows_by_source: dict[str, dict] = {}
            if include_events and before is None and video_dir:
                for source in selected_sources:
                    window = _read_live_hls_window(
                        video_dir, source["id"]
                    )
                    if window:
                        live_windows_by_source[source["id"]] = window

            # Counts populate the sticky object filter on an initial camera
            # selection. Pagination does not repeat the aggregation.
            counts: dict[str, int] = {}
            counted_classes: list[str] = []
            counts_complete = False
            if before is None and include_counts:
                zone_policy_active = bool(
                    selected_zone_uids or exclusions_by_source
                )
                if zone_policy_active:
                    counted_classes = list(classes)
                    if counted_classes:
                        for source in selected_sources:
                            polygons = polygons_by_source.get(source["id"])
                            source_exclusions = exclusions_by_source.get(source["id"])
                            source_counts = video_db.detection_class_counts(
                                source["id"], polygons, True,
                                source_exclusions, counted_classes,
                            )
                            for cls, count in source_counts.items():
                                counts[cls] = counts.get(cls, 0) + count
                else:
                    counts = video_db.detection_class_counts(
                        None if source_id == "all" else source_id,
                        None,
                        True,
                        None,
                        available_classes,
                    )
                    counts_complete = True
                    counted_classes = list(available_classes)
            cameras = []
            if include_events:
                cameras = (
                    [_detection_wall_all(
                        video_db,
                        selected_sources,
                        classes,
                        limit,
                        before,
                        polygons_by_source,
                        live_windows_by_source,
                        exclusions_by_source,
                    )]
                    if source_id == "all"
                    else [_detection_wall_camera(
                        video_db,
                        selected_sources[0],
                        classes,
                        limit,
                        before,
                        polygons_by_source.get(selected_sources[0]["id"]),
                        selected_sources[0]["id"] in polygons_by_source,
                        live_windows_by_source.get(selected_sources[0]["id"]),
                        exclusions_by_source.get(selected_sources[0]["id"]),
                    )]
                )
            return {
                "classes": counts,
                "available_classes": available_classes,
                "counted_classes": counted_classes,
                "class_counts_complete": counts_complete,
                "cameras": cameras,
                "zones": [
                    {
                        "uid": str(zone["uid"]),
                        "source_id": str(zone["source_id"]),
                        "source_name": source_names.get(
                            str(zone["source_id"]), str(zone["source_id"])
                        ),
                        "name": zone.get("name") or f"Area {zone.get('id', '')}",
                    }
                    for zone in available_zone_rows
                ],
                "selected_zones": selected_zone_uids,
                "exclusions": [
                    {
                        "uid": str(zone["uid"]),
                        "source_id": str(zone["source_id"]),
                        "name": zone.get("name") or "Exclusion area",
                    }
                    for zone in exclusion_rows
                ],
                "sources": [
                    {
                        "id": source["id"],
                        "name": source.get("name") or source["id"],
                        "record_mode": source.get(
                            "record_mode", RECORD_MODE_CONTINUOUS
                        ),
                    }
                    for source in all_sources
                ],
                "generated_at": time.time(),
            }

        return JSONResponse(
            await asyncio.to_thread(_payload),
            headers={"Cache-Control": "no-store"},
        )

    async def api_delete_source(request: Request) -> JSONResponse:
        source_id = request.path_params["source_id"]
        if source_db is None:
            return JSONResponse({"error": "db_path not configured"}, status_code=501)
        if source_id not in source_db.ids():
            return JSONResponse({"error": "source not found"}, status_code=404)
        if health_store is not None:
            await asyncio.to_thread(health_store.delete_source, source_id)
        if not source_db.delete(source_id):
            return JSONResponse({"error": "source not found"}, status_code=404)
        if health_collector is not None:
            health_collector.forget_source(source_id)
        await asyncio.to_thread(native_hls.unregister_source_runtime, source_id)
        return JSONResponse({"ok": True})

    async def api_thumb(request: Request) -> Response:
        """Extract a single frame from a video file at timestamp t."""
        if not video_dir:
            return Response(status_code=404)
        event_id = request.query_params.get("event_id")
        if event_id:
            return await _serve_event_thumb(event_id)
        rel = request.query_params.get("path", "")
        t   = float(request.query_params.get("t", 0))
        if ".." in rel or not rel:
            return Response(status_code=400)
        seg_path = (video_dir / rel).resolve()
        if not seg_path.is_file():
            return Response(status_code=404)

        # Disk cache alongside segment
        cache_dir  = seg_path.parent / ".thumbcache"
        cache_file = cache_dir / f"{seg_path.stem}_{t:.1f}.jpg"

        if not cache_file.exists():
            ok = await asyncio.to_thread(_extract_video_thumb, seg_path, cache_file, t)
            if not ok:
                return Response(status_code=404)

        return FileResponse(cache_file, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=604800, immutable"})

    async def _serve_event_thumb(event_id_raw: str) -> Response:
        if not video_dir or not video_db:
            return Response(status_code=404)
        evt = await asyncio.to_thread(video_db.get_event_with_segment, event_id_raw)
        if not evt:
            # Backfill re-inserts detection rows under new ids, orphaning a
            # notification's d:<id> ref. Re-resolve by the notification's own
            # (source, time) — an equivalent detection usually exists.
            evt = await asyncio.to_thread(
                video_db.event_like_for_notification_ref, event_id_raw)
        if not evt:
            return Response(status_code=404)

        seg_path = (video_dir / evt["seg_path"]).resolve()
        try:
            seg_path.relative_to(video_dir.resolve())
        except ValueError:
            return Response(status_code=403)

        thumb_abs_ts = float(evt.get("thumbnail_abs_ts", evt["abs_ts"]))
        t = max(0.0, thumb_abs_ts - float(evt["seg_media_epoch"]))
        try:
            boxes = json.loads(evt["boxes_json"]) if evt.get("boxes_json") else []
        except (TypeError, json.JSONDecodeError):
            boxes = []
        cls = evt.get("class") or ""
        if not cls:
            # d:<id> refs resolve with NULL class (a detection frame can hold
            # several classes at once); the notification knows which one
            # fired. Without it, selection scores by size/confidence and
            # crops the wrong object — a parked car instead of the notified
            # bird.
            cls = await asyncio.to_thread(
                video_db.notification_class_for_ref, event_id_raw) or ""
        box = _select_event_box(boxes, cls)

        async def _try_live_thumb() -> Response | None:
            if not evt.get("provisional") or not box:
                return None
            data = await asyncio.to_thread(
                _extract_live_thumb, video_dir, evt["source_id"], float(evt["abs_ts"]), box)
            if not data:
                return None
            return Response(content=data, media_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

        # Open MP4s are not finalized/readable yet, so use the live HLS fragment
        # while it is still in the DVR window. Closed provisional segments should
        # use their recorded MP4; their HLS fragments may already have rolled off.
        if evt.get("provisional") and evt.get("seg_end_ts") is None:
            live_response = await _try_live_thumb()
            if live_response is not None:
                return live_response

        if not seg_path.is_file():
            live_response = await _try_live_thumb()
            if live_response is not None:
                return live_response
            return Response(status_code=404)

        cache_dir = seg_path.parent / ".thumbcache"
        safe_event_id = "".join(
            ch if ch.isalnum() or ch in {"-", "_"} else "_"
            for ch in event_id_raw
        )
        # v5 also repairs historical rows whose event timestamp and crop box
        # came from different observations in the same track.
        cache_file = cache_dir / f"event_{safe_event_id}_crop_v5.jpg"
        if not cache_file.exists():
            ok = await asyncio.to_thread(_extract_video_thumb, seg_path, cache_file, t, box)
            if not ok:
                live_response = await _try_live_thumb()
                if live_response is not None:
                    return live_response
                return Response(status_code=404)

        # Not immutable: an event thumb can be re-cut under the same URL
        # (dead-ref healing, crop fixes). A day of caching keeps the
        # notification panel cheap without freezing a wrong crop for a week.
        return FileResponse(cache_file, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})

    async def api_video_event_thumb(request: Request) -> Response:
        return await _serve_event_thumb(request.path_params["event_id"])

    async def api_video_live_thumb(request: Request) -> Response:
        """Crop a thumbnail for a provisional (open-segment) event from the
        live HLS .ts fragments, using the frame's SEI clock to find the
        frame closest to the event's time."""
        if not video_dir:
            return Response(status_code=404)
        source_id = request.query_params.get("source") or ""
        if not source_id or ".." in source_id or "/" in source_id:
            return Response(status_code=400)
        try:
            ts = float(request.query_params["ts"])
            x1 = float(request.query_params["x1"])
            y1 = float(request.query_params["y1"])
            x2 = float(request.query_params["x2"])
            y2 = float(request.query_params["y2"])
        except (KeyError, ValueError):
            return Response(status_code=400)

        cache_key = (source_id, round(ts, 2), round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4))
        cached = _live_thumb_cache_get(cache_key)
        if cached is not None:
            if not cached:
                return Response(status_code=404)
            return Response(content=cached, media_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

        box = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "conf": 1.0}
        data = await asyncio.to_thread(_extract_live_thumb, video_dir, source_id, ts, box)
        _live_thumb_cache_put(cache_key, data)
        if not data:
            return Response(status_code=404)
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    async def api_video_segment_at(request: Request) -> JSONResponse:
        """Fast single-segment lookup by timestamp — for instant URL-based seek."""
        if not video_db:
            return JSONResponse({"segment": None})
        try:
            ts = float(request.query_params["ts"])
        except (KeyError, ValueError):
            return JSONResponse({"error": "ts required"}, status_code=400)
        source_id = request.query_params.get("source") or None
        exact = request.query_params.get("exact", "").lower() in {"1", "true", "yes"}
        segment = await asyncio.to_thread(
            video_db.segment_at, source_id, ts, exact=exact)
        return JSONResponse({"segment": segment})

    async def api_video_resolve(request: Request) -> JSONResponse:
        """World time -> media location. The single world<->media boundary.

        See docs/media-time-architecture.md. Callers pass (source, ts) in world
        time and receive {provider, url, media_offset, coverage, media_epoch}
        without doing any offset math themselves.
        """
        if not video_db or not video_dir:
            return JSONResponse({"error": "unavailable"}, status_code=503)
        try:
            ts = float(request.query_params["ts"])
        except (KeyError, ValueError):
            return JSONResponse({"error": "ts required"}, status_code=400)
        source_id = request.query_params.get("source") or None
        if not source_id:
            return JSONResponse({"error": "source required"}, status_code=400)

        from wanyard import media_time

        def _resolve():
            with video_db._connect() as conn:
                return media_time.resolve(conn, video_dir, source_id, ts)

        loc = await asyncio.to_thread(_resolve)
        media_epoch = loc.anchor.media_epoch if loc.anchor else None

        # Recorded media plays the MP4 file directly: currentTime = t - media_epoch.
        # Both t and media_epoch are decoded clock/Unix time; currentTime is only
        # the private player coordinate needed to show that frame.
        return JSONResponse({
            "provider": loc.provider,
            "storage_provider": loc.provider,
            "url": loc.url,
            "media_offset": loc.media_offset,
            "media_epoch": media_epoch,
            "segment_media_epoch": media_epoch,
            "duration": loc.anchor.duration if loc.anchor else None,
            "segment_id": loc.segment_id,
            "source_id": source_id,
            "coverage": ({"start": loc.coverage.start, "end": loc.coverage.end}
                         if loc.coverage else None),
            "reason": loc.reason,
        })

    def _build_timeline(source_id, zone_id=None, since=None, until=None):
        from wanyard.video import _filter_with_polygons
        segs = video_db.list_segments(source_id, since, until)
        bounds = video_db.segment_bounds(source_id)
        summary: dict[int, dict] = {}
        table = (
            "object_events"
            if video_db.object_events_available(source_id, since, until)
            else "video_events"
        )
        episode_filter = "event_type='appeared'" if table == "object_events" else "1"
        polygons = video_db.zone_polygons(source_id, zone_id)
        seg_ids = [s["id"] for s in segs]
        bounded = since is not None or until is not None
        if bounded and seg_ids:
            placeholders = ",".join("?" for _ in seg_ids)
            if polygons:
                where, params = [
                    f"e.{episode_filter}" if table == "object_events" else episode_filter,
                    f"e.segment_id IN ({placeholders})",
                ], list(seg_ids)
                if source_id and source_id != "all":
                    where.append("e.source_id=?")
                    params.append(source_id)
                with video_db._connect() as conn:
                    rows = conn.execute(
                        f"SELECT e.segment_id, e.source_id, e.class, e.boxes_json FROM {table} e"
                        f" WHERE {' AND '.join(where)}",
                        params,
                    ).fetchall()
                for evt in _filter_with_polygons([dict(r) for r in rows], polygons):
                    if evt["segment_id"] is None:
                        continue
                    summary.setdefault(evt["segment_id"], {})[evt["class"]] = (
                        summary.setdefault(evt["segment_id"], {}).get(evt["class"], 0) + 1
                    )
            else:
                where, params = [
                    f"e.{episode_filter}" if table == "object_events" else episode_filter,
                    f"e.segment_id IN ({placeholders})",
                ], list(seg_ids)
                if source_id and source_id != "all":
                    where.append("e.source_id=?")
                    params.append(source_id)
                with video_db._connect() as conn:
                    rows = conn.execute(
                        "SELECT e.segment_id, e.class, COUNT(*) as n"
                        f" FROM {table} e"
                        f" WHERE {' AND '.join(where)}"
                        " GROUP BY e.segment_id, e.class",
                        params,
                    ).fetchall()
                for r in rows:
                    summary.setdefault(r["segment_id"], {})[r["class"]] = r["n"]
        elif bounded:
            pass
        elif polygons:
            where, params = [episode_filter], []
            if source_id and source_id != "all":
                where.append("source_id=?")
                params.append(source_id)
            with video_db._connect() as conn:
                rows = conn.execute(
                    f"SELECT segment_id, source_id, class, boxes_json FROM {table}"
                    f" WHERE {' AND '.join(where)}",
                    params,
                ).fetchall()
            for evt in _filter_with_polygons([dict(r) for r in rows], polygons):
                if evt["segment_id"] is None:
                    continue
                summary.setdefault(evt["segment_id"], {})[evt["class"]] = (
                    summary.setdefault(evt["segment_id"], {}).get(evt["class"], 0) + 1
                )
        else:
            where, params = [episode_filter], []
            if source_id and source_id != "all":
                where.append("source_id=?")
                params.append(source_id)
            with video_db._connect() as conn:
                rows = conn.execute(
                    "SELECT segment_id, class, COUNT(*) as n"
                    f" FROM {table}"
                    f" WHERE {' AND '.join(where)}"
                    " GROUP BY segment_id, class",
                    params,
                ).fetchall()
            for r in rows:
                summary.setdefault(r["segment_id"], {})[r["class"]] = r["n"]
        if until is None or until >= time.time() - 2 * 3600:
            for evt in video_db.provisional_events(source_id, since, zone_id=zone_id):
                if until is not None and evt.get("abs_ts") is not None and evt["abs_ts"] > until:
                    continue
                summary.setdefault(evt["segment_id"], {})[evt["class"]] = (
                    summary.setdefault(evt["segment_id"], {}).get(evt["class"], 0) + 1
                )
        for s in segs:
            s["classes"] = summary.get(s["id"], {})
        return {"segments": segs, "bounds": bounds}

    async def api_video2_timeline(request: Request) -> JSONResponse:
        """Segments list for the video2 filmstrip."""
        if not video_db:
            return JSONResponse({"segments": []})
        source_id = request.query_params.get("source") or None
        zone_id = request.query_params.get("zone") or None
        since_raw = request.query_params.get("since")
        until_raw = request.query_params.get("until")
        try:
            since = float(since_raw) if since_raw else None
            until = float(until_raw) if until_raw else None
        except ValueError:
            return JSONResponse({"error": "invalid since/until"}, status_code=400)
        if since is not None and until is not None and until < since:
            since, until = until, since
        timeline = await asyncio.to_thread(_build_timeline, source_id, zone_id, since, until)
        return JSONResponse(timeline)

    async def api_video_events(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"events": []})
        source_id = request.query_params.get("source") or None
        cls       = request.query_params.get("class")  or None
        classes_raw = request.query_params.get("classes") or None
        date      = request.query_params.get("date")   or None
        # When a time range is provided, since/until bound results — no count limit needed
        _has_range = request.query_params.get("since") and request.query_params.get("until")
        _default = 10**9 if _has_range else 1000
        limit = int(request.query_params.get("limit", _default))
        around_raw = request.query_params.get("around")
        if around_raw:
            classes = None
            if classes_raw:
                classes = [c for c in classes_raw.split(",") if c and c != "all"]
            elif cls and cls != "all":
                classes = [cls]
            zone_id_around = request.query_params.get("zone") or None
            events = await asyncio.to_thread(
                video_db.nearest_events, float(around_raw), source_id, classes, limit, zone_id_around
            )
            provisional = await asyncio.to_thread(
                video_db.provisional_events, source_id, None, zone_id_around
            )
            if classes:
                wanted = set(classes)
                provisional = [e for e in provisional if e["class"] in wanted]
            events = events + provisional
            around = float(around_raw)
            events.sort(key=lambda e: (abs(e["abs_ts"] - around), e["abs_ts"]))
            events = events[:limit]
            for e in events: e.pop("boxes_json", None)
            return JSONResponse({"events": events})
        since_raw = request.query_params.get("since")
        until_raw = request.query_params.get("until")
        since     = float(since_raw) if since_raw else None
        until     = float(until_raw) if until_raw else None
        zone_id   = request.query_params.get("zone") or None
        events = await asyncio.to_thread(
            video_db.list_events, source_id, cls, date, limit, since, until, zone_id
        )
        provisional = await asyncio.to_thread(
            video_db.provisional_events, source_id, since, zone_id
        )
        if cls and cls != "all":
            provisional = [e for e in provisional if e["class"] == cls]
        events = provisional + events
        events.sort(key=lambda e: e["abs_ts"], reverse=True)
        events = events[:limit]
        for e in events: e.pop("boxes_json", None)
        return JSONResponse({"events": events})

    async def api_video_class_counts(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"classes": {}})
        source_id = request.query_params.get("source") or None
        zone_id = request.query_params.get("zone") or None
        counts = await asyncio.to_thread(video_db.class_counts, source_id, True, zone_id)
        return JSONResponse({"classes": counts})

    async def api_video_activity_summary(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"total": 0, "classes": {}})
        source_id = request.query_params.get("source") or None
        zone_id = request.query_params.get("zone") or None
        try:
            since = float(request.query_params["since"])
            until = float(request.query_params["until"])
        except (KeyError, ValueError):
            return JSONResponse({"error": "since and until required"}, status_code=400)
        summary = await asyncio.to_thread(
            video_db.activity_summary, source_id, since, until, zone_id
        )
        return JSONResponse(summary)

    async def api_video_zones(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"zones": []})
        source_id = request.query_params.get("source") or None

        if request.method == "GET":
            # source omitted / "all" → zones across every camera
            lookup = None if not source_id or source_id == "all" else source_id
            zones = await asyncio.to_thread(video_db.list_zones, lookup)
            return JSONResponse({"zones": zones})

        if not source_id or source_id == "all":
            return JSONResponse({"error": "source is required"}, status_code=400)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        zones = body.get("zones", [])
        if not isinstance(zones, list):
            return JSONResponse({"error": "zones must be a list"}, status_code=400)
        try:
            saved = await asyncio.to_thread(video_db.replace_zones, source_id, zones)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"zones": saved})

    async def api_video_zone(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"error": "video db not configured"}, status_code=501)
        source_id = request.query_params.get("source") or None
        zone_uid = request.path_params.get("zone_uid")
        if not source_id or source_id == "all":
            return JSONResponse({"error": "source is required"}, status_code=400)

        if request.method == "DELETE":
            try:
                deleted = await asyncio.to_thread(
                    video_db.delete_zone, source_id, zone_uid
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            if not deleted:
                return JSONResponse({"error": "area not found"}, status_code=404)
            return JSONResponse({"deleted": True})

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        try:
            saved = await asyncio.to_thread(
                video_db.save_zone, source_id, body, zone_uid
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except LookupError:
            return JSONResponse({"error": "area not found"}, status_code=404)
        return JSONResponse({"zone": saved})

    async def api_video_zone_create(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"error": "video db not configured"}, status_code=501)
        source_id = request.query_params.get("source") or None
        if not source_id or source_id == "all":
            return JSONResponse({"error": "source is required"}, status_code=400)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        try:
            saved = await asyncio.to_thread(video_db.save_zone, source_id, body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"zone": saved}, status_code=201)

    async def api_notification_rules(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"error": "video db not configured"}, status_code=501)

        if request.method == "GET":
            source_id = request.query_params.get("source") or None
            rules = await asyncio.to_thread(video_db.list_notification_rules, source_id)
            return JSONResponse({"rules": rules})

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        try:
            rule = await asyncio.to_thread(video_db.create_notification_rule, body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"rule": rule}, status_code=201)

    async def api_notification_rule(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"error": "video db not configured"}, status_code=501)
        try:
            rule_id = int(request.path_params["rule_id"])
        except (KeyError, ValueError):
            return JSONResponse({"error": "invalid rule id"}, status_code=400)

        if request.method == "DELETE":
            deleted = await asyncio.to_thread(video_db.delete_notification_rule, rule_id)
            if not deleted:
                return JSONResponse({"error": "rule not found"}, status_code=404)
            return JSONResponse({"ok": True})

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        try:
            rule = await asyncio.to_thread(video_db.update_notification_rule, rule_id, body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if rule is None:
            return JSONResponse({"error": "rule not found"}, status_code=404)
        return JSONResponse({"rule": rule})

    async def api_notifications(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"notifications": [], "unread_count": 0})
        try:
            limit = int(request.query_params.get("limit", 30))
        except ValueError:
            limit = 30
        unread_only = request.query_params.get("unread") in {"1", "true", "yes"}
        # Materialization runs in the backend loop — this endpoint only reads.
        notifications = await asyncio.to_thread(
            video_db.list_notifications, limit, unread_only
        )
        unread_count = await asyncio.to_thread(video_db.unread_notification_count)
        return JSONResponse({
            "notifications": notifications,
            "unread_count": unread_count,
        })

    async def api_notifications_unread_count(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"unread_count": 0})
        # Materialization runs in the backend loop — this endpoint only reads.
        unread_count = await asyncio.to_thread(video_db.unread_notification_count)
        return JSONResponse({"unread_count": unread_count})

    async def api_notification_read(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"error": "video db not configured"}, status_code=501)
        try:
            notification_id = int(request.path_params["notification_id"])
        except (KeyError, ValueError):
            return JSONResponse({"error": "invalid notification id"}, status_code=400)
        notification = await asyncio.to_thread(
            video_db.mark_notification_read, notification_id
        )
        if notification is None:
            return JSONResponse({"error": "notification not found"}, status_code=404)
        unread_count = await asyncio.to_thread(video_db.unread_notification_count)
        return JSONResponse({
            "notification": notification,
            "unread_count": unread_count,
        })

    async def api_notification_thumb(request: Request) -> Response:
        if not video_db:
            return Response(status_code=404)
        try:
            notification_id = int(request.path_params["notification_id"])
        except (KeyError, ValueError):
            return Response(status_code=400)
        data = await asyncio.to_thread(video_db.get_notification_thumb, notification_id)
        if not data:
            return Response(status_code=404)
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": _IMG_CACHE})

    async def api_notifications_read_all(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"error": "video db not configured"}, status_code=501)
        updated = await asyncio.to_thread(video_db.mark_all_notifications_read)
        unread_count = await asyncio.to_thread(video_db.unread_notification_count)
        return JSONResponse({"updated": updated, "unread_count": unread_count})

    def _source_statuses() -> dict:
        sources = _sources_list(config, source_db)
        now = time.time()
        statuses = {}
        if not video_db:
            return {
                s["id"]: {"state": "offline", "last_ts": None, "age_seconds": None}
                for s in sources
            }
        with video_db._connect() as conn:
            rows = conn.execute(
                "SELECT source_id, MAX(COALESCE(end_ts, start_ts)) as last_ts"
                " FROM segments GROUP BY source_id"
            ).fetchall()
            live_rows = conn.execute(
                "SELECT source_id, MAX(start_ts) as start_ts FROM segments"
                " WHERE end_ts IS NULL AND start_ts>=? GROUP BY source_id",
                (now - 3600,),
            ).fetchall()
        last = {r["source_id"]: r["last_ts"] for r in rows}
        live = {r["source_id"]: r["start_ts"] for r in live_rows}
        for src in sources:
            sid = src["id"]
            hls = (video_dir / "live" / sid / "live.m3u8") if video_dir else None
            hls_age = None
            if hls and hls.exists():
                try:
                    hls_age = now - hls.stat().st_mtime
                except OSError:
                    hls_age = None
            if sid in live and hls_age is not None and hls_age <= 12:
                state = "live"
            elif sid in live:
                state = "buffering"
            elif last.get(sid) and now - float(last[sid]) < 900:
                state = "buffering"
            else:
                state = "offline"
            statuses[sid] = {
                "state": state,
                "last_ts": last.get(sid),
                "age_seconds": (now - float(last[sid])) if last.get(sid) else None,
                "hls_age_seconds": hls_age,
            }
        return statuses

    async def api_video_source_status(request: Request) -> JSONResponse:
        return JSONResponse({"sources": await asyncio.to_thread(_source_statuses)})

    async def api_video_segments(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"segments": []})
        source_id = request.query_params.get("source") or None
        since_raw = request.query_params.get("since")
        until_raw = request.query_params.get("until")
        try:
            since = float(since_raw) if since_raw else None
            until = float(until_raw) if until_raw else None
        except ValueError:
            return JSONResponse({"error": "invalid since/until"}, status_code=400)
        if since is not None and until is not None and until < since:
            since, until = until, since
        segs = await asyncio.to_thread(video_db.list_segments, source_id, since, until)
        return JSONResponse({"segments": segs})

    async def api_video_detections(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"detections": []})
        seg_id = request.query_params.get("segment_id")
        if not seg_id:
            return JSONResponse({"error": "segment_id required"}, status_code=400)
        dets = await asyncio.to_thread(video_db.detections_for_segment, int(seg_id))
        return JSONResponse({"detections": dets})

    async def api_video_overlays(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"detections": []})
        source_id = request.query_params.get("source") or None
        if not source_id or source_id == "all":
            return JSONResponse({"error": "source required"}, status_code=400)
        try:
            since = float(request.query_params["since"])
            until = float(request.query_params["until"])
        except (KeyError, ValueError):
            return JSONResponse({"error": "since/until required"}, status_code=400)
        if until < since:
            since, until = until, since
        if until - since > 900:
            return JSONResponse({"error": "overlay window too large"}, status_code=400)
        dets = await asyncio.to_thread(
            video_db.detections_between, source_id, since, until)
        return JSONResponse({"detections": dets})

    async def api_video_live_status(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"segments": [], "events": [], "detections": []})
        source_id = request.query_params.get("source") or None
        zone_id = request.query_params.get("zone") or None
        try:
            det_since = _optional_float_query(request, "det_since")
            det_until = _optional_float_query(request, "det_until")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if det_since is not None and det_until is not None:
            if det_until < det_since:
                det_since, det_until = det_until, det_since
            if det_until - det_since > 120:
                return JSONResponse({"error": "detection window too large"}, status_code=400)
        status = await asyncio.to_thread(
            video_db.live_status, source_id, zone_id, det_since, det_until)
        return JSONResponse(status)

    async def api_video_live_window(request: Request) -> JSONResponse:
        source_id = request.query_params.get("source") or None
        window = await asyncio.to_thread(_read_live_hls_window, video_dir, source_id)
        return JSONResponse({"window": window})

    async def api_video_native_live(request: Request) -> JSONResponse:
        source_id = request.query_params.get("source") or None
        if not native_hls.safe_path_part(source_id):
            return JSONResponse({"native": None})
        if not native_hls.base_url():
            return JSONResponse({"native": None})
        known = {s["id"] for s in _sources_list(config, source_db)}
        if known and source_id not in known:
            return JSONResponse({"native": None}, status_code=404)
        source_path = native_hls.source_path(source_id)
        return JSONResponse({
            "native": {
                "source_id": source_id,
                "path": source_path,
                "url": native_hls.public_manifest_url(source_id),
            }
        })

    def _class_filter(raw: str | None) -> set[str]:
        if not raw:
            return set()
        return {c.strip() for c in raw.split(",") if c.strip()}

    def _box_color(cls: str) -> str:
        colors = {
            "person": "0x4ec98a",
            "bird": "0x78b7ff",
            "cat": "0x78b7ff",
            "dog": "0x78b7ff",
            "car": "0xe8a558",
            "truck": "0xf1788a",
            "bus": "0xcc9bff",
            "motorcycle": "0x7bd7c4",
            "bicycle": "0xd6ca72",
        }
        if cls in colors:
            return colors[cls]
        palette = ["0x78b7ff", "0x4ec98a", "0xe8a558", "0xcc9bff", "0xf1788a", "0x7bd7c4", "0xd6ca72"]
        h = 0
        for ch in cls:
            h = ((h << 5) - h + ord(ch)) & 0xffffffff
        return palette[abs(h) % len(palette)]

    def _drawtext_escape(text: str) -> str:
        return (
            text
            .replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace(":", "\\:")
            .replace(",", "\\,")
        )

    def _clip_box_filter(seg: dict, clip_start: float, clip_end: float,
                         include_classes: set[str], exclude_classes: set[str]) -> str | None:
        if not video_db:
            return None
        try:
            dets = video_db.detections_for_segment(int(seg["id"]))
        except (KeyError, TypeError, ValueError):
            return None
        if not dets:
            return None

        duration = max(0.0, clip_end - clip_start)
        samples = []
        for det in dets:
            try:
                rel = float(det["abs_ts"]) - clip_start
            except (KeyError, TypeError, ValueError):
                continue
            if rel < -1.5 or rel > duration + 1.5:
                continue
            boxes = []
            for box in det.get("boxes") or []:
                if not isinstance(box, dict):
                    continue
                try:
                    x1 = max(0.0, min(1.0, float(box["x1"])))
                    y1 = max(0.0, min(1.0, float(box["y1"])))
                    x2 = max(0.0, min(1.0, float(box["x2"])))
                    y2 = max(0.0, min(1.0, float(box["y2"])))
                    conf = float(box.get("conf") or 0.0)
                except (KeyError, TypeError, ValueError):
                    continue
                if x2 <= x1 or y2 <= y1:
                    continue
                # tracklets are built unfiltered (assoc is per-class); class filter
                # is applied per tracklet below, matching the browser overlay.
                boxes.append({"cls": str(box.get("cls") or ""), "conf": conf,
                              "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                              "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2})
            if boxes:
                samples.append((rel, boxes))

        if not samples:
            return None
        samples.sort(key=lambda item: item[0])

        def _lerp_expr(v0, v1, t0, t1):
            # linear-in-t ffmpeg expression so the burned box glides a->b
            if abs(t1 - t0) < 1e-6 or abs(v1 - v0) < 1e-9:
                return f"{v0:.6f}"
            return f"({v0:.6f}+({(v1 - v0):.6f})*(t-{t0:.6f})/{(t1 - t0):.6f})"

        def _emit(a, b, cls, conf, t0, t1):
            color = _box_color(cls)
            ex1 = _lerp_expr(a["x1"], b["x1"], t0, t1)
            ey1 = _lerp_expr(a["y1"], b["y1"], t0, t1)
            ex2 = _lerp_expr(a["x2"], b["x2"], t0, t1)
            ey2 = _lerp_expr(a["y2"], b["y2"], t0, t1)
            enable = f"between(t\\,{t0:.3f}\\,{t1:.3f})"
            label = f"{cls} {round(conf * 100)}%" if conf > 0 else cls
            label_y = f"h*{ey1}-30" if a["y1"] > 0.03 else f"h*{ey2}+2"
            return [
                "drawbox="
                f"x=iw*{ex1}:y=ih*{ey1}:"
                f"w=iw*({ex2}-{ex1}):h=ih*({ey2}-{ey1}):"
                f"color={color}@0.95:t=3:enable='{enable}'",
                "drawtext="
                "expansion=none:"
                f"text='{_drawtext_escape(label)}':"
                f"x=w*{ex1}:y={label_y}:"
                "fontcolor=0x050709:fontsize=24:"
                f"box=1:boxcolor={color}@0.95:boxborderw=5:"
                f"enable='{enable}'",
            ]

        filters = []
        for tr in _overlay_tracklets(samples):
            cls = tr["cls"]
            if include_classes and cls not in include_classes:
                continue
            if exclude_classes and cls in exclude_classes:
                continue
            pts = tr["pts"]
            for i in range(len(pts)):
                a = pts[i]["box"]
                conf = float(a.get("conf") or 0.0)
                nxt = pts[i + 1] if i + 1 < len(pts) else None
                if nxt is not None and (nxt["t"] - pts[i]["t"]) <= _OVL_MAX_GAP:
                    t0 = max(0.0, pts[i]["t"]); t1 = min(duration, nxt["t"])
                    if t1 > t0:
                        filters += _emit(a, nxt["box"], cls, conf, t0, t1)
                else:
                    # tail / lone / gap: persist this box forward (causal), no lerp
                    t0 = max(0.0, pts[i]["t"]); t1 = min(duration, pts[i]["t"] + _OVL_SNAP)
                    if t1 > t0:
                        filters += _emit(a, a, cls, conf, t0, t1)
                if len(filters) >= 2500:
                    return ",".join(filters)
        return ",".join(filters) if filters else None

    def _export_clip(source_id: str | None, ts: float,
                     before: float, after: float, *,
                     overlay_boxes: bool = False,
                     include_classes: set[str] | None = None,
                     exclude_classes: set[str] | None = None) -> tuple[Path | None, Path | None, str | None]:
        if not video_dir or not video_db:
            return None, None, "video is not configured"
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None, None, "ffmpeg is not available"
        start_ts, end_ts = ts - before, ts + after
        segs = video_db.segments_overlapping(source_id, start_ts, end_ts)
        if not segs:
            return None, None, "no recorded video for that range"

        tmpdir = Path(tempfile.mkdtemp(prefix="wanyard_clip_"))
        parts: list[Path] = []
        root = video_dir.resolve()
        for i, seg in enumerate(segs):
            seg_path = (video_dir / seg["path"]).resolve()
            try:
                seg_path.relative_to(root)
            except ValueError:
                continue
            if not seg_path.is_file():
                continue
            seg_epoch = float(seg["media_epoch"])
            seg_dur = (float(seg["duration_sec"]) if seg["duration_sec"] is not None
                       else float(seg["end_ts"]) - float(seg["start_ts"]))
            clip_start = max(start_ts, seg_epoch)
            clip_end = min(end_ts, seg_epoch + seg_dur)
            if clip_end <= clip_start:
                continue
            out = tmpdir / f"part_{i:03d}.mp4"
            vf = (
                _clip_box_filter(seg, clip_start, clip_end, include_classes or set(), exclude_classes or set())
                if overlay_boxes else None
            )
            filter_file = None
            if vf:
                filter_file = tmpdir / f"filters_{i:03d}.txt"
                filter_file.write_text(vf, encoding="utf-8")
            cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{max(0.0, clip_start - seg_epoch):.3f}",
                "-i", str(seg_path),
                "-t", f"{clip_end - clip_start:.3f}",
                "-map", "0:v:0?", "-map", "0:a:0?",
            ]
            if filter_file:
                cmd += ["-filter_script:v:0", str(filter_file)]
            cmd += [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-movflags", "+faststart",
                str(out),
            ]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=90, check=False)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
                parts.append(out)

        if not parts:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None, None, "could not export clip"
        if len(parts) == 1:
            return parts[0], tmpdir, None

        list_file = tmpdir / "parts.txt"
        list_file.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
        out = tmpdir / "clip.mp4"
        try:
            r = subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "concat", "-safe", "0", "-i", str(list_file),
                 "-c", "copy", "-movflags", "+faststart", str(out)],
                capture_output=True, timeout=90, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None, None, "could not stitch clip"
        if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None, None, "could not stitch clip"
        return out, tmpdir, None

    async def api_video_clip(request: Request) -> Response:
        try:
            ts = float(request.query_params["ts"])
            before = min(300.0, max(0.0, float(request.query_params.get("before", 30))))
            after = min(300.0, max(0.0, float(request.query_params.get("after", 30))))
        except (KeyError, ValueError):
            return JSONResponse({"error": "ts is required"}, status_code=400)
        source_id = request.query_params.get("source") or None
        overlay_boxes = request.query_params.get("boxes") in {"1", "true", "yes", "on"}
        include_classes = _class_filter(request.query_params.get("classes"))
        exclude_classes = _class_filter(request.query_params.get("exclude_classes"))
        path, tmpdir, error = await asyncio.to_thread(
            _export_clip, source_id, ts, before, after,
            overlay_boxes=overlay_boxes,
            include_classes=include_classes,
            exclude_classes=exclude_classes,
        )
        if error or not path or not tmpdir:
            return JSONResponse({"error": error or "could not export clip"}, status_code=404)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
        filename = f"cam-viewer-{source_id or 'all'}-{stamp}.mp4"
        response = FileResponse(
            path,
            media_type="video/mp4",
            filename=filename,
            background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
            headers={"Cache-Control": "no-store"},
        )
        token = request.query_params.get("download_token")
        if token:
            response.set_cookie(
                "wanyard_clip_download",
                token,
                max_age=60,
                path="/",
                samesite="lax",
            )
        return response

    # Frame-clock builds are full-file packet scans (seconds for a legacy
    # large media file). Scrubbing across old segments can request many at
    # once, and an aborted HTTP fetch does NOT stop a to_thread scan — a
    # short drag once queued a dozen parallel scans that pinned the disk and
    # starved every other request. Dedup per path (concurrent requesters
    # await one build) and serialize globally (one scan owns the disk).
    _clock_builds: dict[str, asyncio.Future] = {}
    _clock_build_gate = asyncio.Semaphore(1)

    async def _ensure_frame_clock(media_path) -> None:
        key = str(media_path)
        fut = _clock_builds.get(key)
        if fut is None:
            async def _build() -> None:
                from .video import _write_mp4_frame_clock
                async with _clock_build_gate:
                    await asyncio.to_thread(_write_mp4_frame_clock, media_path)
            fut = asyncio.ensure_future(_build())
            _clock_builds[key] = fut
            fut.add_done_callback(lambda _f: _clock_builds.pop(key, None))
        try:
            await fut
        except Exception:
            LOG.warning("frame clock build failed for %s", media_path,
                        exc_info=True)

    async def serve_video_file(request: Request) -> Response:
        if not video_dir:
            return Response(status_code=404)
        rel = unquote(request.path_params["path"])
        if ".." in rel:
            return Response(status_code=403)
        path = (video_dir / rel).resolve()
        if not path.is_file() and path.name.endswith(".mp4.clock.json"):
            media_path = path.with_name(path.name[:-len(".clock.json")])
            if media_path.is_file() and media_path.suffix.lower() == ".mp4":
                await _ensure_frame_clock(media_path)
        if not path.is_file():
            return Response(status_code=404)
        suffix = path.suffix.lower()
        media = {
            "mp4": "video/mp4", "jpg": "image/jpeg", "vtt": "text/vtt",
            "json": "application/json",
        }.get(suffix[1:])
        headers = {"Accept-Ranges": "bytes"}
        if suffix in {".mp4", ".json"}:
            # A young mtime means the file can still change (open fMP4 being
            # written, sidecar just re-cut, salvage/finalize rewrite) — do not
            # cache. Anything older is sealed and its URL is unique per
            # segment timestamp: cache hard, scrubbing re-visits ranges
            # constantly. Retention deleting the file later just 404s.
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                age = 0.0
            headers["Cache-Control"] = (
                "no-cache" if age < 120
                else "public, max-age=2592000, immutable"
            )
        return FileResponse(path, media_type=media, headers=headers)

    async def api_settings_status(request: Request) -> JSONResponse:
        import shutil as _shutil
        disk = _shutil.disk_usage(video_dir or Path("."))
        pending = 0
        ignored_invalid = 0
        total_segs = 0
        latest_event_ts = None
        if video_db:
            with video_db._connect() as conn:
                pending = conn.execute(
                    "SELECT COUNT(*) FROM segments s WHERE s.end_ts IS NOT NULL"
                    " AND s.media_epoch IS NOT NULL"
                    " AND s.scanned_at IS NULL"
                ).fetchone()[0]
                ignored_invalid = conn.execute(
                    "SELECT COUNT(*) FROM segments s WHERE s.end_ts IS NOT NULL"
                    " AND s.media_epoch IS NULL"
                    " AND s.scanned_at IS NULL"
                ).fetchone()[0]
                total_segs = conn.execute(
                    "SELECT COUNT(*) FROM segments WHERE end_ts IS NOT NULL"
                ).fetchone()[0]
                row = conn.execute(
                    "SELECT MAX(abs_ts) FROM ("
                    " SELECT abs_ts FROM video_events"
                    " UNION ALL"
                    " SELECT abs_ts FROM object_events"
                    ")"
                ).fetchone()
                latest_event_ts = row[0] if row else None
        # Check yolo-serve socket
        yolo_ok = False
        backfill_alive = False
        try:
            import socket as _sock, json as _json
            s = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(os.environ.get("YOLO_SOCKET", "/tmp/yolo.sock"))
            s.sendall(b'{"type":"ping"}\n')
            resp = _json.loads(s.recv(256).decode())
            s.close()
            yolo_ok = resp.get("status") == "ok"
            backfill_alive = bool(resp.get("backfill_alive"))
        except Exception:
            pass
        # Disk usage per source
        source_sizes = {}
        if video_dir:
            for src_dir in video_dir.iterdir():
                if src_dir.is_dir() and not src_dir.name.startswith(".") and src_dir.name != "live":
                    try:
                        total = sum(f.stat().st_size for f in src_dir.rglob("*.mp4"))
                        source_sizes[src_dir.name] = total
                    except Exception:
                        pass
        recording_threads = capture_worker.thread_health() if capture_worker else {}
        recording_workers = capture_worker.recorder_status() if capture_worker else {}
        return JSONResponse({
            "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
            "video_dir": str(video_dir) if video_dir else None,
            "source_sizes": source_sizes,
            "segments": total_segs,
            "backfill_pending": pending,
            "backfill_ignored_invalid": ignored_invalid,
            "yolo_connected": yolo_ok,
            "backfill_alive": backfill_alive,
            "recording_threads": recording_threads,
            "recording_workers": recording_workers,
            "latest_event_ts": latest_event_ts,
        })

    async def api_settings_media_health(request: Request) -> JSONResponse:
        if health_store is None:
            return JSONResponse(
                {"error": "media health database unavailable"}, status_code=503
            )
        try:
            hours = float(request.query_params.get("hours", "24"))
        except ValueError:
            return JSONResponse({"error": "hours must be a number"}, status_code=400)
        hours = min(24 * 14, max(1.0, hours))
        source_id = request.query_params.get("source") or None
        source_ids = {source["id"] for source in _sources_list(config, source_db)}
        if source_id and source_id not in source_ids:
            return JSONResponse({"error": "source not found"}, status_code=404)
        await asyncio.to_thread(
            health_store.prune_sources, list(source_ids)
        )
        data = await asyncio.to_thread(
            health_store.snapshot,
            since=time.time() - hours * 3600,
            source_id=source_id,
        )
        data["generated_at"] = time.time()
        data["hours"] = hours
        data["sources"] = _sources_list(config, source_db)
        return JSONResponse(data)

    async def api_settings_camera_test(request: Request) -> Response:
        """Grab a single frame from an RTSP URL and return it as JPEG."""
        try:
            body = await request.json()
            url  = str(body.get("url", "")).strip()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not url.startswith(("rtsp://", "rtsps://")):
            return JSONResponse({"error": "url must start with rtsp://"}, status_code=400)
        import tempfile as _tmp
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return JSONResponse({"error": "ffmpeg not available"}, status_code=500)
        with _tmp.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            out = Path(tf.name)
        try:
            r = subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                 "-rtsp_transport", "tcp", "-i", url,
                 "-frames:v", "1", "-q:v", "5", str(out)],
                capture_output=True, timeout=10, check=False,
            )
            if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                return JSONResponse({"error": "could not connect or read frame"}, status_code=502)
            return Response(out.read_bytes(), media_type="image/jpeg")
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "connection timed out"}, status_code=504)
        finally:
            out.unlink(missing_ok=True)

    async def api_settings_detection_config(request: Request) -> JSONResponse:
        """Get or update the YOLO class whitelist used by live and backfill."""
        if not video_db:
            return JSONResponse(
                {"error": "video db not configured"}, status_code=501
            )
        if request.method == "GET":
            return JSONResponse(detection_settings_payload(video_db))
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "request body must be a JSON object"}, status_code=400
            )
        try:
            save_detection_classes(video_db, body.get("classes"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        payload = detection_settings_payload(video_db)
        payload["applies_within_seconds"] = 3
        return JSONResponse(payload)

    def _ntfy_payload(request: Request) -> dict:
        config = load_ntfy_config(
            video_db,
            default_base_url=str(request.base_url).rstrip("/"),
        )
        status = video_db.notification_delivery_status(
            "ntfy", config.destination_key
        )
        return config.public_payload(status)

    async def api_settings_ntfy(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse(
                {"error": "video db not configured"}, status_code=501
            )
        if request.method == "GET":
            return JSONResponse(
                await asyncio.to_thread(_ntfy_payload, request)
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "request body must be a JSON object"},
                status_code=400,
            )
        try:
            await asyncio.to_thread(
                save_ntfy_config,
                video_db,
                body,
                default_base_url=str(request.base_url).rstrip("/"),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(await asyncio.to_thread(_ntfy_payload, request))

    async def api_settings_ntfy_test(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse(
                {"error": "video db not configured"}, status_code=501
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "request body must be a JSON object"},
                status_code=400,
            )
        try:
            config = await asyncio.to_thread(
                save_ntfy_config,
                video_db,
                body,
                default_base_url=str(request.base_url).rstrip("/"),
            )
            remote_id = await asyncio.to_thread(send_ntfy_test, config)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except NtfyPublishError as exc:
            return JSONResponse(
                {"error": str(exc), "status_code": exc.status_code},
                status_code=502,
            )
        return JSONResponse({
            "ok": True,
            "remote_id": remote_id,
            "settings": await asyncio.to_thread(_ntfy_payload, request),
        })

    async def api_settings_cleanup_config(request: Request) -> JSONResponse:
        """Get or update auto-cleanup thresholds and per-camera retention.

        Global ``cleanup_days``/``cleanup_max_gb`` plus per-camera max-age
        overrides (``day_overrides``, video db) and record modes
        (``record_modes``, source db: continuous | live_only). No reload
        plumbing: the recorder watchdog and stamper path refresh both pick up
        record-mode flips within ~30s.
        """
        if not video_db:
            return JSONResponse({"error": "video db not configured"}, status_code=501)

        def _payload() -> dict:
            sources = _sources_list(config, source_db)
            if source_db is not None:
                return retention_settings_payload(source_db, video_db, sources)
            return {
                "cleanup_days": retention_normalize_days(video_db.get_setting("cleanup_days")),
                "cleanup_max_gb": video_db.get_setting("cleanup_max_gb"),
                "day_overrides": source_cleanup_days(video_db),
                "record_modes": {},
                "effective_days": {},
                "sources": sources,
            }

        if request.method == "GET":
            return JSONResponse(_payload())
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "request body must be a JSON object"}, status_code=400
            )

        source_ids = {s["id"] for s in _sources_list(config, source_db)}
        day_overrides = body.get("day_overrides") or {}
        record_modes = body.get("record_modes") or {}
        for field, mapping in (("day_overrides", day_overrides),
                               ("record_modes", record_modes)):
            if not isinstance(mapping, dict):
                return JSONResponse(
                    {"error": f"{field} must be an object"}, status_code=400
                )
            unknown = sorted(set(mapping) - source_ids)
            if unknown:
                return JSONResponse(
                    {"error": f"unknown camera: {unknown[0]}"}, status_code=400
                )
        if record_modes and source_db is None:
            return JSONResponse({"error": "db_path not configured"}, status_code=501)

        # Validate everything before writing anything.
        parsed_days: dict[str, float | None] = {}
        for sid, value in day_overrides.items():
            if value in (None, "", "global"):
                parsed_days[sid] = None
                continue
            days = retention_normalize_days(value)
            if days is None:
                return JSONResponse(
                    {"error": f"invalid max age for {sid}"}, status_code=400
                )
            parsed_days[sid] = days
        parsed_modes: dict[str, str | None] = {}
        for sid, value in record_modes.items():
            if value in (None, "", "global"):
                parsed_modes[sid] = None
                continue
            try:
                parsed_modes[sid] = validate_record_mode(value)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

        if "cleanup_days" in body:
            v = body["cleanup_days"]
            if v is None:
                with video_db._connect() as c: c.execute("DELETE FROM app_settings WHERE key='cleanup_days'")
            else:
                video_db.set_setting("cleanup_days", float(v))
        if "cleanup_max_gb" in body:
            v = body["cleanup_max_gb"]
            if v is None:
                with video_db._connect() as c: c.execute("DELETE FROM app_settings WHERE key='cleanup_max_gb'")
            else:
                video_db.set_setting("cleanup_max_gb", float(v))
        for sid, days in parsed_days.items():
            if days is None:
                with video_db._connect() as c:
                    c.execute("DELETE FROM app_settings WHERE key=?",
                              (cleanup_days_key(sid),))
            else:
                video_db.set_setting(cleanup_days_key(sid), days)
        for sid, mode in parsed_modes.items():
            if mode is None or mode == RECORD_MODE_CONTINUOUS:
                source_db.delete_setting(record_mode_key(sid))
            else:
                source_db.set_setting(record_mode_key(sid), mode)
        return JSONResponse(_payload())

    async def api_settings_cleanup(request: Request) -> JSONResponse:
        """Delete footage and notifications older than N days."""
        if not video_db or not video_dir:
            return JSONResponse({"error": "video not configured"}, status_code=501)
        try:
            body  = await request.json()
            days  = int(body.get("days", 30))
            src   = body.get("source_id") or None
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        cutoff = time.time() - days * 86400
        result = await asyncio.to_thread(
            delete_before, video_db, video_dir, cutoff, src
        )
        return JSONResponse(result)

    async def serve_live_hls(request: Request) -> Response:
        source_id = request.path_params.get("source_id", "")
        filename  = request.path_params.get("filename", "")
        if not video_dir or not source_id or ".." in source_id or ".." in filename:
            return Response(status_code=404)
        path = video_dir / "live" / source_id / filename
        if not path.exists():
            return Response(status_code=404)
        is_m3u8 = filename.endswith(".m3u8")
        media = "application/vnd.apple.mpegurl" if is_m3u8 else "video/mp2t"
        if is_m3u8:
            try:
                content = path.read_bytes()
            except OSError:
                return Response(status_code=404)
            return Response(content=content, media_type=media,
                            headers={"Cache-Control": "no-cache"})
        return FileResponse(path, media_type=media,
                            headers={"Cache-Control": "no-cache, no-store"})

    async def serve_native_live_hls(request: Request) -> Response:
        source_path = request.path_params.get("source_path", "")
        asset = request.path_params.get("asset", "")
        upstream = native_hls.upstream_url(
            source_path,
            asset,
            request.url.query,
        )
        if not upstream:
            return Response(status_code=404)
        headers = {}
        range_header = request.headers.get("range")
        if range_header:
            headers["Range"] = range_header
        try:
            status, body, content_type, response_headers = await asyncio.to_thread(
                native_hls.fetch_asset,
                upstream,
                headers,
            )
        except urllib.error.HTTPError as exc:
            return Response(status_code=exc.code)
        except (urllib.error.URLError, TimeoutError, OSError):
            return Response(status_code=502)
        # iOS/Safari play through the OS HLS engine, which can't sustain the
        # LL-HLS edge through this proxy (stalls blank after a few seconds).
        # Strip the partial-segment directives so they get plain live HLS.
        if asset.endswith(".m3u8") and native_hls.prefers_native_player(
            request.headers.get("user-agent")
        ):
            body = native_hls.strip_low_latency(body)
        return Response(
            content=body,
            media_type=native_hls.media_type(asset, content_type),
            status_code=status,
            headers={"Cache-Control": "no-cache, no-store", **response_headers},
        )

    async def serve_webrtc_whep(request: Request) -> Response:
        # Proxy the WHEP SDP exchange to go2rtc (signaling only — media flows
        # direct browser<->go2rtc over ICE). go2rtc keeps its API internal; only
        # its WebRTC media port is public. Used by the wall for instant live.
        source_path = request.path_params.get("source_path", "")
        url = native_hls.go2rtc_whep_url(source_path)
        if not url:
            return Response(status_code=404)
        body = await request.body()
        try:
            status, answer, ctype = await asyncio.to_thread(native_hls.post_sdp, url, body)
        except urllib.error.HTTPError as exc:
            return Response(status_code=exc.code)
        except (urllib.error.URLError, TimeoutError, OSError):
            return Response(status_code=502)
        # Point the WebRTC host candidate at the address the browser used to
        # reach us (Host header) — reachable by definition. Override with
        # WANYARD_WEBRTC_ADDITIONAL_HOSTS for topologies where it isn't.
        try:
            sdp = answer.decode() if isinstance(answer, bytes) else answer
            override = os.environ.get("WANYARD_WEBRTC_ADDITIONAL_HOSTS", "").split(",")[0].strip()
            host = override or request.headers.get("host", "").rsplit(":", 1)[0]
            if host:
                answer = native_hls.rewrite_host_candidates(sdp, native_hls.resolve_host(host))
        except Exception:
            pass
        return Response(
            content=answer,
            media_type=(ctype or "application/sdp").split(";", 1)[0].strip(),
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )

    routes = [
        Route("/video/webrtc/{source_path}/whep", serve_webrtc_whep, methods=["POST"]),
        # Landing = the live wall (god view). A specific ?source (and not the
        # explicit ?view=wall) loads the full single-camera viewer instead, so a
        # tile's /?source=X&live=1&… link opens the viewer. ?source=all keeps the
        # viewer's aggregate timeline.
        Route("/", lambda r: FileResponse(
            static_dir / ("video2.html"
                          if r.query_params.get("source") and r.query_params.get("view") != "wall"
                          else "wall.html"),
            headers={"Cache-Control": "no-cache"})),
        Route("/detections",               lambda r: FileResponse(static_dir / "detections.html", headers={"Cache-Control": "no-cache"})),
        Route("/settings",                  lambda r: FileResponse(static_dir / "settings.html", headers={"Cache-Control": "no-cache"})),
        Route("/api/health",                api_health),
        Route("/api/thumb",                 api_thumb),
        Route("/api/video/event-thumb/{event_id}", api_video_event_thumb),
        Route("/api/video/live-thumb",      api_video_live_thumb),
        Route("/api/video/segment-at",      api_video_segment_at),
        Route("/api/video/resolve",         api_video_resolve),
        Route("/api/video2/timeline",       api_video2_timeline),
        Route("/api/video/events",          api_video_events),
        Route("/api/video/classes",         api_video_class_counts),
        Route("/api/video/activity-summary", api_video_activity_summary),
        Route("/api/detections/wall",       api_detection_wall),
        Route("/api/video/zones",           api_video_zones, methods=["GET", "PUT"]),
        Route("/api/video/zones/new",       api_video_zone_create, methods=["POST"]),
        Route("/api/video/zones/{zone_uid}", api_video_zone, methods=["PUT", "DELETE"]),
        Route("/api/notifications",         api_notifications),
        Route("/api/notifications/unread-count", api_notifications_unread_count),
        Route("/api/notifications/read-all", api_notifications_read_all, methods=["POST"]),
        Route("/api/notifications/{notification_id}/thumb", api_notification_thumb),
        Route("/api/notifications/{notification_id}/read", api_notification_read, methods=["POST"]),
        Route("/api/notifications/rules",   api_notification_rules, methods=["GET", "POST"]),
        Route("/api/notifications/rules/{rule_id}", api_notification_rule, methods=["PUT", "DELETE"]),
        Route("/api/video/segments",        api_video_segments),
        Route("/api/video/detections",      api_video_detections),
        Route("/api/video/overlays",        api_video_overlays),
        Route("/api/video/live",            api_video_live_status),
        Route("/api/video/live-window",     api_video_live_window),
        Route("/api/video/native-live",     api_video_native_live),
        Route("/api/video/source-status",   api_video_source_status),
        Route("/api/video/clip",            api_video_clip),
        Route("/video/files/{path:path}",   serve_video_file),
        Route("/video/live/{source_id}/{filename}", serve_live_hls),
        Route("/video/native-live/{source_path}/{asset}", serve_native_live_hls),
        Route("/api/sources",                    api_sources,             methods=["GET", "POST"]),
        Route("/api/sources/{source_id}",        api_delete_source,       methods=["DELETE"]),
        Route("/api/settings/status",            api_settings_status),
        Route("/api/settings/media-health",      api_settings_media_health),
        Route("/api/settings/camera/test",       api_settings_camera_test, methods=["POST"]),
        Route("/api/settings/detection-config",  api_settings_detection_config, methods=["GET", "POST"]),
        Route("/api/settings/ntfy",              api_settings_ntfy, methods=["GET", "POST"]),
        Route("/api/settings/ntfy/test",         api_settings_ntfy_test, methods=["POST"]),
        Route("/api/settings/cleanup-config",    api_settings_cleanup_config, methods=["GET", "POST"]),
        Route("/api/settings/cleanup",           api_settings_cleanup,     methods=["POST"]),
        Mount("/", StaticFiles(directory=static_dir, html=True)),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    return _PathAwareGZipMiddleware(app, minimum_size=1024,
                                    skip_prefixes=_GZIP_SKIP_PREFIXES,
                                    skip_suffixes=_GZIP_SKIP_SUFFIXES)


# ── helpers ───────────────────────────────────────────────


def _sources_list(config: AppConfig, source_db=None) -> list:
    sources = source_db.to_source_configs() if source_db else []
    return [
        {
            "id": s.id, "name": s.name or s.id, "type": s.type,
            "enabled": s.enabled,
            "interval_seconds": s.interval_seconds,
            "mutable": True,
            # live_only = realtime wall only: no recorder, no stamper, no
            # detection. The viewer uses this to explain the missing DVR.
            "record_mode": (
                retention_record_mode(source_db, s.id)
                if source_db else RECORD_MODE_CONTINUOUS
            ),
        }
        for s in sources if s.type == "rtsp"
    ]
