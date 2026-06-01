from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from starlette.background import BackgroundTask
from starlette.applications import Starlette
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .config import AppConfig

_THUMB_W  = 160
_IMG_CACHE = "public, max-age=604800, immutable"
_GZIP_SKIP_PREFIXES = ("/video/live/",)


class _PathAwareGZipMiddleware:
    def __init__(self, app, *, minimum_size: int, skip_prefixes: tuple[str, ...]):
        self.app = app
        self.gzip_app = GZipMiddleware(app, minimum_size=minimum_size)
        self.skip_prefixes = skip_prefixes

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if any(path.startswith(prefix) for prefix in self.skip_prefixes):
                await self.app(scope, receive, send)
                return
        await self.gzip_app(scope, receive, send)


def _parse_hls_program_date_time(raw: str) -> float | None:
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    if len(value) >= 5 and value[-5] in "+-" and value[-3] != ":":
        value = value[:-2] + ":" + value[-2:]
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _read_live_hls_window(video_dir: Path | None, source_id: str | None) -> dict | None:
    if not video_dir or not source_id or ".." in source_id:
        return None
    playlist = video_dir / "live" / source_id / "live.m3u8"
    try:
        stat = playlist.stat()
        lines = playlist.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    target_duration = 0.0
    next_start: float | None = None
    next_duration: float | None = None
    inferred_start: float | None = None
    segments: list[dict] = []

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target_duration = float(line.split(":", 1)[1])
            except (IndexError, ValueError):
                target_duration = 0.0
        elif line.startswith("#EXTINF:"):
            try:
                next_duration = float(line.split(":", 1)[1].split(",", 1)[0])
            except (IndexError, ValueError):
                next_duration = None
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            next_start = _parse_hls_program_date_time(line.split(":", 1)[1])
        elif not line.startswith("#"):
            duration = next_duration if next_duration is not None else target_duration
            start = next_start if next_start is not None else inferred_start
            if start is not None and duration > 0:
                end = start + duration
                segments.append({
                    "uri": Path(line.split("?", 1)[0]).name,
                    "start_ts": start,
                    "end_ts": end,
                    "duration": duration,
                })
                inferred_start = end
            next_start = None
            next_duration = None

    if not segments:
        return None
    start_ts = segments[0]["start_ts"]
    end_ts = max(s["end_ts"] for s in segments)
    return {
        "source_id": source_id,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "duration": max(0.0, end_ts - start_ts),
        "segment_count": len(segments),
        "playlist_age_seconds": max(0.0, time.time() - stat.st_mtime),
        "segments": segments,
    }


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
    matching = [b for b in candidates if b.get("cls") == cls] or candidates

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
                "scale=176:132:force_original_aspect_ratio=increase,"
                "crop=176:132"
            )

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    for attempt_t in [t, max(0, t - 1), max(0, t - 2), max(0, t - 5)]:
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(attempt_t), "-i", str(seg_path),
        ]
        if vf:
            cmd += ["-vf", vf]
        cmd += ["-frames:v", "1", "-q:v", "5", str(cache_file)]
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



def make_app(
    config: AppConfig,
    source_db=None,
    video_dir=None,
    video_db=None,
    capture_worker=None,
) -> Starlette:
    import wanyard
    static_dir = Path(wanyard.__file__).parent / "static"

    @asynccontextmanager
    async def lifespan(app: Starlette):
        if capture_worker:
            capture_worker.start()
        try:
            yield
        finally:
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

    async def api_delete_source(request: Request) -> JSONResponse:
        source_id = request.path_params["source_id"]
        if source_db is None:
            return JSONResponse({"error": "db_path not configured"}, status_code=501)
        if not source_db.delete(source_id):
            return JSONResponse({"error": "source not found"}, status_code=404)
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
            return Response(status_code=404)

        seg_path = (video_dir / evt["seg_path"]).resolve()
        try:
            seg_path.relative_to(video_dir.resolve())
        except ValueError:
            return Response(status_code=403)
        if not seg_path.is_file():
            return Response(status_code=404)

        t = max(0.0, float(evt["abs_ts"]) - float(evt["seg_start_ts"]))
        try:
            boxes = json.loads(evt["boxes_json"]) if evt.get("boxes_json") else []
        except (TypeError, json.JSONDecodeError):
            boxes = []
        box = _select_event_box(boxes, evt.get("class", ""))

        cache_dir = seg_path.parent / ".thumbcache"
        safe_event_id = "".join(
            ch if ch.isalnum() or ch in {"-", "_"} else "_"
            for ch in event_id_raw
        )
        cache_file = cache_dir / f"event_{safe_event_id}_crop_v1.jpg"
        if not cache_file.exists():
            ok = await asyncio.to_thread(_extract_video_thumb, seg_path, cache_file, t, box)
            if not ok:
                return Response(status_code=404)

        return FileResponse(cache_file, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=604800, immutable"})

    async def api_video_event_thumb(request: Request) -> Response:
        return await _serve_event_thumb(request.path_params["event_id"])

    async def api_video_hls_thumb(request: Request) -> Response:
        if not video_db:
            return Response(status_code=404)
        hls_id = request.path_params.get("hls_id", "")
        try:
            hls_id_int = int(hls_id)
        except ValueError:
            return Response(status_code=400)
        data = await asyncio.to_thread(video_db.get_hls_thumb, hls_id_int)
        if not data:
            return Response(status_code=404)
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": _IMG_CACHE})

    async def api_video_segment_at(request: Request) -> JSONResponse:
        """Fast single-segment lookup by timestamp — for instant URL-based seek."""
        if not video_db:
            return JSONResponse({"segment": None})
        try:
            ts = float(request.query_params["ts"])
        except (KeyError, ValueError):
            return JSONResponse({"error": "ts required"}, status_code=400)
        source_id = request.query_params.get("source") or None
        with video_db._connect() as conn:
            where = "start_ts <= ? AND end_ts > ? AND end_ts IS NOT NULL"
            params = [ts, ts]
            if source_id:
                where += " AND source_id = ?"
                params.append(source_id)
            row = conn.execute(
                f"SELECT * FROM segments WHERE {where} ORDER BY start_ts DESC LIMIT 1",
                params
            ).fetchone()
            if not row:
                # Nearest closed segment before ts
                where2 = "end_ts IS NOT NULL AND end_ts <= ?"
                params2 = [ts]
                if source_id:
                    where2 += " AND source_id = ?"
                    params2.append(source_id)
                row = conn.execute(
                    f"SELECT * FROM segments WHERE {where2} ORDER BY end_ts DESC LIMIT 1",
                    params2
                ).fetchone()
        return JSONResponse({"segment": dict(row) if row else None})

    def _build_timeline(source_id, zone_id=None):
        from wanyard.video import _filter_with_polygons
        segs = video_db.list_segments(source_id)
        summary: dict[int, dict] = {}
        table = "object_events" if video_db.object_events_available(source_id) else "video_events"
        episode_filter = "event_type='appeared'" if table == "object_events" else "1"
        polygons = video_db.zone_polygons(source_id, zone_id)
        if polygons:
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
        for evt in video_db.provisional_events(source_id, zone_id=zone_id):
            summary.setdefault(evt["segment_id"], {})[evt["class"]] = (
                summary.setdefault(evt["segment_id"], {}).get(evt["class"], 0) + 1
            )
        for s in segs:
            s["classes"] = summary.get(s["id"], {})
        return segs

    async def api_video2_timeline(request: Request) -> JSONResponse:
        """Segments list for the video2 filmstrip."""
        if not video_db:
            return JSONResponse({"segments": []})
        source_id = request.query_params.get("source") or None
        zone_id = request.query_params.get("zone") or None
        segs = await asyncio.to_thread(_build_timeline, source_id, zone_id)
        return JSONResponse({"segments": segs})

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
        segs = await asyncio.to_thread(video_db.list_segments, source_id)
        return JSONResponse({"segments": segs})

    async def api_video_detections(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"detections": []})
        seg_id = request.query_params.get("segment_id")
        if not seg_id:
            return JSONResponse({"error": "segment_id required"}, status_code=400)
        dets = await asyncio.to_thread(video_db.detections_for_segment, int(seg_id))
        return JSONResponse({"detections": dets})

    async def api_video_live_status(request: Request) -> JSONResponse:
        if not video_db:
            return JSONResponse({"segments": [], "events": [], "detections": []})
        source_id = request.query_params.get("source") or None
        zone_id = request.query_params.get("zone") or None
        status = await asyncio.to_thread(video_db.live_status, source_id, zone_id)
        return JSONResponse(status)

    async def api_video_live_window(request: Request) -> JSONResponse:
        source_id = request.query_params.get("source") or None
        window = await asyncio.to_thread(_read_live_hls_window, video_dir, source_id)
        return JSONResponse({"window": window})

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
        seg_start = float(seg["start_ts"])
        samples: list[tuple[float, list[tuple[str, float, float, float, float, float]]]] = []
        for det in dets:
            try:
                rel = seg_start + float(det.get("ts_offset") or 0.0) - clip_start
            except (TypeError, ValueError):
                continue
            if rel < -1.5 or rel > duration + 1.5:
                continue
            boxes = []
            for box in det.get("boxes") or []:
                if not isinstance(box, dict):
                    continue
                cls = str(box.get("cls") or "")
                if include_classes and cls not in include_classes:
                    continue
                if exclude_classes and cls in exclude_classes:
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
                boxes.append((cls, conf, x1, y1, x2, y2))
            if boxes:
                samples.append((rel, boxes))

        if not samples:
            return None
        samples.sort(key=lambda item: item[0])

        filters = []
        for i, (rel, boxes) in enumerate(samples):
            prev_rel = samples[i - 1][0] if i > 0 else None
            next_rel = samples[i + 1][0] if i + 1 < len(samples) else None
            start = rel - min(1.5, (rel - prev_rel) / 2) if prev_rel is not None else rel - 1.5
            end = rel + min(1.5, (next_rel - rel) / 2) if next_rel is not None else rel + 1.5
            start = max(0.0, start)
            end = min(duration, end)
            if end <= start:
                continue
            enable = f"between(t\\,{start:.3f}\\,{end:.3f})"
            for cls, conf, x1, y1, x2, y2 in boxes:
                color = _box_color(cls)
                label = cls
                if conf > 0:
                    label = f"{cls} {round(conf * 100)}%"
                label_y = f"h*{y1:.6f}-30" if y1 > 0.03 else f"h*{y2:.6f}+2"
                filters.append(
                    "drawbox="
                    f"x=iw*{x1:.6f}:y=ih*{y1:.6f}:"
                    f"w=iw*{(x2 - x1):.6f}:h=ih*{(y2 - y1):.6f}:"
                    f"color={color}@0.95:t=3:enable='{enable}'"
                )
                filters.append(
                    "drawtext="
                    "expansion=none:"
                    f"text='{_drawtext_escape(label)}':"
                    f"x=w*{x1:.6f}:y={label_y}:"
                    "fontcolor=0x050709:fontsize=24:"
                    f"box=1:boxcolor={color}@0.95:boxborderw=5:"
                    f"enable='{enable}'"
                )
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
            clip_start = max(start_ts, float(seg["start_ts"]))
            clip_end = min(end_ts, float(seg["end_ts"]))
            if clip_end <= clip_start:
                continue
            out = tmpdir / f"part_{i:03d}.mp4"
            vf = (
                _clip_box_filter(seg, clip_start, clip_end, include_classes or set(), exclude_classes or set())
                if overlay_boxes else None
            )
            cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{max(0.0, clip_start - float(seg['start_ts'])):.3f}",
                "-i", str(seg_path),
                "-t", f"{clip_end - clip_start:.3f}",
                "-map", "0:v:0?", "-map", "0:a:0?",
            ]
            if vf:
                cmd += ["-vf", vf]
            cmd += [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-movflags", "+faststart",
                str(out),
            ]
            r = subprocess.run(cmd, capture_output=True, timeout=90, check=False)
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
        r = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(list_file),
             "-c", "copy", "-movflags", "+faststart", str(out)],
            capture_output=True, timeout=90, check=False,
        )
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

    async def serve_video_file(request: Request) -> Response:
        if not video_dir:
            return Response(status_code=404)
        rel = unquote(request.path_params["path"])
        if ".." in rel:
            return Response(status_code=403)
        path = (video_dir / rel).resolve()
        if not path.is_file():
            return Response(status_code=404)
        suffix = path.suffix.lower()
        media = {"mp4": "video/mp4", "jpg": "image/jpeg", "vtt": "text/vtt"}.get(suffix[1:])
        headers = {"Accept-Ranges": "bytes"}
        if suffix == ".mp4":
            headers["Cache-Control"] = "no-cache"
        return FileResponse(path, media_type=media, headers=headers)

    async def api_settings_status(request: Request) -> JSONResponse:
        import shutil as _shutil
        disk = _shutil.disk_usage(video_dir or Path("."))
        pending = 0
        total_segs = 0
        latest_event_ts = None
        if video_db:
            with video_db._connect() as conn:
                pending = conn.execute(
                    "SELECT COUNT(*) FROM segments s WHERE s.end_ts IS NOT NULL"
                    " AND NOT EXISTS (SELECT 1 FROM video_detections WHERE segment_id=s.id)"
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
        return JSONResponse({
            "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
            "video_dir": str(video_dir) if video_dir else None,
            "source_sizes": source_sizes,
            "segments": total_segs,
            "backfill_pending": pending,
            "yolo_connected": yolo_ok,
            "backfill_alive": backfill_alive,
            "recording_threads": recording_threads,
            "latest_event_ts": latest_event_ts,
        })

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

    async def api_settings_cleanup_config(request: Request) -> JSONResponse:
        """Get or update auto-cleanup thresholds (stored in DB, read by yolo-serve)."""
        if not video_db:
            return JSONResponse({"error": "video db not configured"}, status_code=501)
        if request.method == "GET":
            days = video_db.get_setting("cleanup_days")
            gb   = video_db.get_setting("cleanup_max_gb")
            return JSONResponse({"cleanup_days": days, "cleanup_max_gb": gb})
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
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
        days = video_db.get_setting("cleanup_days")
        gb   = video_db.get_setting("cleanup_max_gb")
        return JSONResponse({"cleanup_days": days, "cleanup_max_gb": gb})

    async def api_settings_cleanup(request: Request) -> JSONResponse:
        """Delete segments (and their data) older than N days."""
        if not video_db or not video_dir:
            return JSONResponse({"error": "video not configured"}, status_code=501)
        try:
            body  = await request.json()
            days  = int(body.get("days", 30))
            src   = body.get("source_id") or None
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        cutoff = __import__("time").time() - days * 86400
        with video_db._connect() as conn:
            where = "end_ts IS NOT NULL AND end_ts < ?"
            params: list = [cutoff]
            if src:
                where += " AND source_id = ?"
                params.append(src)
            segs = [dict(r) for r in conn.execute(
                f"SELECT id, path FROM segments WHERE {where}", params
            ).fetchall()]
        deleted_files = deleted_bytes = 0
        seg_ids = []
        for seg in segs:
            seg_ids.append(seg["id"])
            p = video_dir / seg["path"]
            try:
                if p.exists():
                    deleted_bytes += p.stat().st_size
                    p.unlink()
                    deleted_files += 1
                # Remove spritesheet dir
                sprite_dir = p.with_suffix("")
                if sprite_dir.is_dir():
                    import shutil as _sh
                    _sh.rmtree(sprite_dir, ignore_errors=True)
            except Exception:
                pass
        if seg_ids:
            with video_db._connect() as conn:
                placeholders = ",".join("?" * len(seg_ids))
                conn.execute(f"DELETE FROM video_events WHERE segment_id IN ({placeholders})", seg_ids)
                conn.execute(f"DELETE FROM object_events WHERE segment_id IN ({placeholders})", seg_ids)
                conn.execute(f"DELETE FROM video_detections WHERE segment_id IN ({placeholders})", seg_ids)
                conn.execute(f"DELETE FROM segments WHERE id IN ({placeholders})", seg_ids)
        return JSONResponse({
            "deleted_segments": len(seg_ids),
            "deleted_files": deleted_files,
            "freed_bytes": deleted_bytes,
        })

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

    routes = [
        Route("/",                           lambda r: FileResponse(static_dir / "video2.html", headers={"Cache-Control": "no-cache"})),
        Route("/settings",                  lambda r: FileResponse(static_dir / "settings.html", headers={"Cache-Control": "no-cache"})),
        Route("/api/health",                api_health),
        Route("/api/thumb",                 api_thumb),
        Route("/api/video/event-thumb/{event_id}", api_video_event_thumb),
        Route("/api/video/hls-thumb/{hls_id}", api_video_hls_thumb),
        Route("/api/video/segment-at",      api_video_segment_at),
        Route("/api/video2/timeline",       api_video2_timeline),
        Route("/api/video/events",          api_video_events),
        Route("/api/video/classes",         api_video_class_counts),
        Route("/api/video/activity-summary", api_video_activity_summary),
        Route("/api/video/zones",           api_video_zones, methods=["GET", "PUT"]),
        Route("/api/video/segments",        api_video_segments),
        Route("/api/video/detections",      api_video_detections),
        Route("/api/video/live",            api_video_live_status),
        Route("/api/video/live-window",     api_video_live_window),
        Route("/api/video/source-status",   api_video_source_status),
        Route("/api/video/clip",            api_video_clip),
        Route("/video/files/{path:path}",   serve_video_file),
        Route("/video/live/{source_id}/{filename}", serve_live_hls),
        Route("/api/sources",                    api_sources,             methods=["GET", "POST"]),
        Route("/api/sources/{source_id}",        api_delete_source,       methods=["DELETE"]),
        Route("/api/settings/status",            api_settings_status),
        Route("/api/settings/camera/test",       api_settings_camera_test, methods=["POST"]),
        Route("/api/settings/cleanup-config",    api_settings_cleanup_config, methods=["GET", "POST"]),
        Route("/api/settings/cleanup",           api_settings_cleanup,     methods=["POST"]),
        Mount("/", StaticFiles(directory=static_dir, html=True)),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    return _PathAwareGZipMiddleware(app, minimum_size=1024,
                                    skip_prefixes=_GZIP_SKIP_PREFIXES)


# ── helpers ───────────────────────────────────────────────


def _sources_list(config: AppConfig, source_db=None) -> list:
    sources = source_db.to_source_configs() if source_db else []
    return [
        {
            "id": s.id, "name": s.name or s.id, "type": s.type,
            "enabled": s.enabled,
            "interval_seconds": s.interval_seconds,
            "mutable": True,
        }
        for s in sources if s.type == "rtsp"
    ]
