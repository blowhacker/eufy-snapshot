"""Standalone YOLO inference server.

Runs as a separate process/container. Owns:
  - YOLO model loading
  - Backfill loop: tags untagged segments and extracts events
  - Unix socket server for future live frame requests

Start with: wanyard yolo-serve
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import socketserver
import threading
import time
from pathlib import Path

LOG = logging.getLogger(__name__)

SOCKET_PATH = os.environ.get("YOLO_SOCKET", "/tmp/yolo.sock")
_CONFIRMATION_STRATEGY = "yolo1280-crop640-960-v1"
_CONFIRM_FULL_IMGSZ = 1280
_CONFIRM_CROP_IMGSZ = (640, 960)
_CONFIRM_MIN_CONF = 0.25
_CONFIRM_MIN_IOU = 0.10
_CONFIRM_MAX_CENTER_DISTANCE = 0.060


# ── Socket server ──────────────────────────────────────────────────────────────

class _YoloHandler(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                resp = self.server.dispatch(req)
            except Exception as e:
                resp = {"status": "error", "error": str(e)}
            self.wfile.write((json.dumps(resp) + "\n").encode())
            self.wfile.flush()


class YoloSocketServer(socketserver.ThreadingUnixStreamServer):
    def __init__(self, socket_path: str, model, video_db, video_dir, predict_lock,
                 class_selector=None):
        self.model            = model
        self.video_db         = video_db
        self.video_dir        = video_dir
        self.predict_lock     = predict_lock
        self.class_selector   = class_selector
        self._backfill_thread: threading.Thread | None = None
        if Path(socket_path).exists():
            Path(socket_path).unlink()
        super().__init__(socket_path, _YoloHandler)

    def dispatch(self, req: dict) -> dict:
        t = req.get("type")
        if t == "ping":
            bt = self._backfill_thread
            return {"status": "ok", "backfill_alive": bool(bt and bt.is_alive())}
        if t == "status":
            bt = self._backfill_thread
            return {"status": "ok",
                    "model": str(getattr(self.model, "model_name", None)),
                    "classes": (
                        self.class_selector.ids()
                        if self.class_selector is not None else None
                    ),
                    "backfill_alive": bool(bt and bt.is_alive())}
        if t == "confirm_notification_event":
            return _confirm_notification_event(
                self.model, self.predict_lock, self.video_db, self.video_dir, req
            )
        return {"status": "error", "error": f"unknown type: {t}"}


# ── Per-class thumb crop (matches MP4 _select_event_box + _crop_from_box) ─────

def _crop_thumb(frame, cls_boxes: list, cls: str,
                thumb_w: int = 176, thumb_h: int = 132,
                aspect: float = 4 / 3) -> bytes | None:
    """Crop the frame around the best box for this class, resize to thumb_w×thumb_h."""
    import cv2
    if not cls_boxes:
        return None

    # Pick best box by (confidence, area)
    def score(b):
        try:
            area = max(0.0, float(b["x2"]) - float(b["x1"])) * \
                   max(0.0, float(b["y2"]) - float(b["y1"]))
            conf = float(b.get("conf", 0.0))
        except (KeyError, TypeError, ValueError):
            return (0.0, 0.0)
        return (conf, area)
    box = max(cls_boxes, key=score)

    fh, fw = frame.shape[:2]
    try:
        x1 = max(0.0, min(1.0, float(box["x1"]))) * fw
        y1 = max(0.0, min(1.0, float(box["y1"]))) * fh
        x2 = max(0.0, min(1.0, float(box["x2"]))) * fw
        y2 = max(0.0, min(1.0, float(box["y2"]))) * fh
    except (KeyError, TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None

    bw, bh = x2 - x1, y2 - y1
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    pad = max(24.0, max(bw, bh) * 0.45)
    cw, ch = bw + pad * 2, bh + pad * 2
    if cw / ch < aspect: cw = ch * aspect
    else:                ch = cw / aspect
    cw = min(float(fw), max(96.0, cw))
    ch = min(float(fh), max(72.0, ch))
    if cw / ch < aspect: cw = min(float(fw), ch * aspect)
    else:                ch = min(float(fh), cw / aspect)
    rw = min(fw, max(2, round(cw)))
    rh = min(fh, max(2, round(ch)))
    x = int(max(0.0, min(float(fw - rw), cx - rw / 2)))
    y = int(max(0.0, min(float(fh - rh), cy - rh / 2)))

    cropped = frame[y:y+rh, x:x+rw]
    if cropped.size == 0:
        return None
    small = cv2.resize(cropped, (thumb_w, thumb_h))
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return buf.tobytes() if ok else None


# ── Notification confirmation ─────────────────────────────────────────────────

def _confirm_notification_event(model, predict_lock, video_db, video_dir: Path, req: dict) -> dict:
    cls = str(req.get("class") or "").strip()
    cls_id = _cctv_class_id(cls)
    if cls_id is None:
        return {"status": "error", "error": f"unsupported class: {cls}"}
    source_id = str(req.get("source_id") or "").strip()
    try:
        abs_ts = float(req.get("abs_ts"))
    except (TypeError, ValueError):
        return {"status": "error", "error": "invalid abs_ts"}
    candidates = _candidate_boxes(req.get("boxes_json"), cls)
    if not candidates:
        return {
            "status": "ok",
            "strategy_version": _CONFIRMATION_STRATEGY,
            "confirmed": False,
            "confidence": 0.0,
            "reason": "no_candidate_box",
        }

    frame, frame_source, frame_error = _frame_for_confirmation(
        video_db, video_dir, source_id, abs_ts, str(req.get("event_kind") or "")
    )
    if frame is None:
        return {
            "status": "ok",
            "strategy_version": _CONFIRMATION_STRATEGY,
            "confirmed": False,
            "confidence": 0.0,
            "reason": "frame_unavailable",
            "frame_source": frame_source,
            "frame_error": frame_error,
        }

    timings: dict[str, float] = {}
    full_boxes, timings["full_1280"] = _predict_boxes(
        model, predict_lock, frame, cls_id, _CONFIRM_FULL_IMGSZ
    )
    full_match = _best_match(full_boxes, candidates)
    if _match_confirmed(full_match):
        return {
            "status": "ok",
            "strategy_version": _CONFIRMATION_STRATEGY,
            "confirmed": True,
            "confidence": full_match["conf"],
            "reason": "full_1280",
            "box": _response_box(full_match),
            "frame_source": frame_source,
            "full_confidence": full_match["conf"],
            "crop_confidence": 0.0,
            "timings_ms": timings,
        }

    crop_best: dict | None = None
    for idx, candidate in enumerate(candidates):
        crop, offset = _crop_for_box(frame, candidate)
        if crop is None:
            continue
        for imgsz in _CONFIRM_CROP_IMGSZ:
            boxes, ms = _predict_boxes(
                model,
                predict_lock,
                crop,
                cls_id,
                imgsz,
                offset_xy=offset,
                full_wh=(frame.shape[1], frame.shape[0]),
            )
            timings[f"crop{idx}_{imgsz}"] = ms
            match = _best_match(boxes, [candidate])
            if match and (crop_best is None or match["conf"] > crop_best["conf"]):
                crop_best = match
    if _match_confirmed(crop_best):
        return {
            "status": "ok",
            "strategy_version": _CONFIRMATION_STRATEGY,
            "confirmed": True,
            "confidence": crop_best["conf"],
            "reason": "crop_640_960",
            "box": _response_box(crop_best),
            "frame_source": frame_source,
            "full_confidence": full_match["conf"] if full_match else 0.0,
            "crop_confidence": crop_best["conf"],
            "timings_ms": timings,
        }

    return {
        "status": "ok",
        "strategy_version": _CONFIRMATION_STRATEGY,
        "confirmed": False,
        "confidence": max(
            full_match["conf"] if full_match else 0.0,
            crop_best["conf"] if crop_best else 0.0,
        ),
        "reason": "no_high_res_match",
        "box": _response_box(crop_best or full_match),
        "frame_source": frame_source,
        "full_confidence": full_match["conf"] if full_match else 0.0,
        "crop_confidence": crop_best["conf"] if crop_best else 0.0,
        "timings_ms": timings,
    }


def _cctv_class_id(cls: str) -> int | None:
    from .video import _CCTV_CLASSES
    for cls_id, name in _CCTV_CLASSES.items():
        if name == cls:
            return int(cls_id)
    return None


def _candidate_boxes(raw, cls: str) -> list[dict]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raw = []
    if not isinstance(raw, list):
        return []
    boxes = []
    for box in raw:
        if not isinstance(box, dict):
            continue
        if str(box.get("cls") or cls) != cls:
            continue
        try:
            x1 = max(0.0, min(1.0, float(box["x1"])))
            y1 = max(0.0, min(1.0, float(box["y1"])))
            x2 = max(0.0, min(1.0, float(box["x2"])))
            y2 = max(0.0, min(1.0, float(box["y2"])))
        except (KeyError, TypeError, ValueError):
            continue
        if x2 <= x1 or y2 <= y1:
            continue
        boxes.append({
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "conf": float(box.get("conf") or 0.0),
            "cls": cls,
        })
    boxes.sort(key=lambda b: (b["conf"], _box_area(b)), reverse=True)
    return boxes


def _frame_for_confirmation(video_db, video_dir: Path, source_id: str, abs_ts: float, _event_kind: str):
    from . import media_time
    with video_db._connect() as conn:
        result = media_time.read_frame(conn, video_dir, source_id, abs_ts)
    if result.frame is not None:
        return result.frame, result.provider, None
    detail = result.status
    if result.retry_after is not None:
        detail = f"{detail}:retry_after={result.retry_after:.3f}"
    return None, result.provider, detail


def _predict_boxes(
    model,
    predict_lock,
    frame,
    cls_id: int,
    imgsz: int,
    *,
    offset_xy: tuple[int, int] | None = None,
    full_wh: tuple[int, int] | None = None,
) -> tuple[list[dict], float]:
    from .video import _parse_results
    start = time.perf_counter()
    with predict_lock:
        results = model.predict(frame, classes=[cls_id], conf=0.01, imgsz=imgsz, verbose=False)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    _, _, boxes = _parse_results(results)
    if offset_xy is None or full_wh is None:
        return boxes, elapsed_ms
    crop_h, crop_w = frame.shape[:2]
    full_w, full_h = full_wh
    off_x, off_y = offset_xy
    mapped = []
    for box in boxes:
        mapped.append({
            **box,
            "x1": (off_x + float(box["x1"]) * crop_w) / full_w,
            "y1": (off_y + float(box["y1"]) * crop_h) / full_h,
            "x2": (off_x + float(box["x2"]) * crop_w) / full_w,
            "y2": (off_y + float(box["y2"]) * crop_h) / full_h,
        })
    return mapped, elapsed_ms


def _crop_for_box(frame, box: dict):
    h, w = frame.shape[:2]
    x1 = float(box["x1"]) * w
    y1 = float(box["y1"]) * h
    x2 = float(box["x2"]) * w
    y2 = float(box["y2"]) * h
    bw = max(1.0, x2 - x1)
    bh = max(1.0, y2 - y1)
    cx = (x1 + x2) / 2
    cy = (y1 + y2) / 2
    size = max(max(bw, bh) * 3.0, min(w, h) * 0.12)
    nx1 = max(0, int(round(cx - size / 2)))
    ny1 = max(0, int(round(cy - size / 2)))
    nx2 = min(w, int(round(cx + size / 2)))
    ny2 = min(h, int(round(cy + size / 2)))
    if nx2 <= nx1 or ny2 <= ny1:
        return None, None
    return frame[ny1:ny2, nx1:nx2].copy(), (nx1, ny1)


def _best_match(detections: list[dict], candidates: list[dict]) -> dict | None:
    best = None
    for det in detections:
        for candidate in candidates:
            iou = _box_iou(det, candidate)
            center_distance = _center_distance(det, candidate)
            geometry_ok = (
                iou >= _CONFIRM_MIN_IOU
                or center_distance <= _CONFIRM_MAX_CENTER_DISTANCE
            )
            if not geometry_ok:
                continue
            match = {
                **det,
                "iou": iou,
                "center_distance": center_distance,
            }
            if best is None or (match["conf"], match["iou"]) > (best["conf"], best["iou"]):
                best = match
    return best


def _match_confirmed(match: dict | None) -> bool:
    return bool(match and float(match.get("conf") or 0.0) >= _CONFIRM_MIN_CONF)


def _box_iou(a: dict, b: dict) -> float:
    ix1 = max(float(a["x1"]), float(b["x1"]))
    iy1 = max(float(a["y1"]), float(b["y1"]))
    ix2 = min(float(a["x2"]), float(b["x2"]))
    iy2 = min(float(a["y2"]), float(b["y2"]))
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    intersection = iw * ih
    union = _box_area(a) + _box_area(b) - intersection
    return intersection / union if union > 0 else 0.0


def _box_area(box: dict) -> float:
    return max(0.0, float(box["x2"]) - float(box["x1"])) * \
        max(0.0, float(box["y2"]) - float(box["y1"]))


def _center_distance(a: dict, b: dict) -> float:
    acx = (float(a["x1"]) + float(a["x2"])) / 2
    acy = (float(a["y1"]) + float(a["y2"])) / 2
    bcx = (float(b["x1"]) + float(b["x2"])) / 2
    bcy = (float(b["y1"]) + float(b["y2"])) / 2
    return ((acx - bcx) ** 2 + (acy - bcy) ** 2) ** 0.5


def _response_box(box: dict | None) -> dict | None:
    if not box:
        return None
    keys = ("x1", "y1", "x2", "y2", "conf", "iou", "center_distance")
    return {k: round(float(box[k]), 6) for k in keys if k in box}


# ── Backfill loop ──────────────────────────────────────────────────────────────

def _backfill_loop(model, video_db, video_dir: Path, stop_event: threading.Event, predict_lock):
    from .video import _yolo_tag_video, extract_events

    LOG.info("backfill loop started")
    while not stop_event.is_set():
        try:
            with video_db._connect() as conn:
                segs = conn.execute(
                    "SELECT s.* FROM segments s WHERE s.end_ts IS NOT NULL"
                    " AND s.media_epoch IS NOT NULL"
                    " AND s.scanned_at IS NULL"
                    " ORDER BY s.start_ts"
                    " LIMIT 5"
                ).fetchall()

            if not segs:
                stop_event.wait(15)
                continue

            for row in segs:
                if stop_event.is_set():
                    break
                seg = dict(row)
                seg_path = video_dir / seg["path"]

                # B4: backfill is gap-filler only (segments the live detector
                # did not claim — scanned_at IS NULL). The old HLS-covered
                # re-tag branch is gone with hls_events (that was the
                # double-YOLO). Just YOLO the recorded MP4.
                if seg_path.exists():
                    n = _yolo_tag_video(
                        model, seg_path, seg["id"], video_db, predict_lock
                    )
                    LOG.info("backfilled %d frames: %s", n, seg["path"][-35:])
                else:
                    video_db.mark_scanned(seg["id"])
                dets = video_db.detections_for_segment(seg["id"])
                n_evt = extract_events(seg, dets, video_db)
                if n_evt:
                    LOG.info("extracted %d events: %s", n_evt, seg["path"][-35:])
        except Exception:
            LOG.exception("backfill error — retrying in 30s")
            stop_event.wait(30)

    LOG.info("backfill loop stopped")


# ── Auto-cleanup loop ──────────────────────────────────────────────────────────

def _cleanup_loop(video_db, video_dir: Path, stop_event: threading.Event):
    """Periodically delete old footage based on CLEANUP_DAYS / CLEANUP_MAX_GB.

    ``cleanup_days`` is the global max age; per-camera overrides
    (``cleanup_days_source:<id>``) win over it, and a camera with neither is
    never age-pruned. ``cleanup_max_gb`` stays global — disk pressure is a
    host property — deleting oldest-overall regardless of source. Thresholds
    are re-read every cycle, so the loop must keep running even when none are
    set yet (settings can appear at runtime).
    """
    from .retention import delete_segments, normalize_days, source_cleanup_days

    def _get_thresholds():
        # DB overrides env vars
        days = video_db.get_setting("cleanup_days")
        gb   = video_db.get_setting("cleanup_max_gb")
        if days is None:
            d = os.environ.get("CLEANUP_DAYS", "")
            days = float(d) if d else None
        if gb is None:
            g = os.environ.get("CLEANUP_MAX_GB", "")
            gb = float(g) if g else None
        return normalize_days(days), gb

    LOG.info("auto-cleanup loop started")
    while not stop_event.is_set():
        try:
            cleanup_days, cleanup_gb = _get_thresholds()
            day_overrides = source_cleanup_days(video_db)
            now = time.time()

            total_used = sum(
                f.stat().st_size for f in video_dir.rglob("*.mp4") if f.is_file()
            ) if cleanup_gb else 0
            over_gb = bool(cleanup_gb) and total_used > cleanup_gb * 1e9

            # Per-source age horizon (override wins over global). Notification
            # expiry uses the same horizons, scoped by source so a 1-day
            # camera's cleanup can never wipe a 30-day camera's notifications.
            override_ids = sorted(day_overrides)
            horizons = {sid: now - days * 86400
                        for sid, days in day_overrides.items()}

            segs = []
            with video_db._connect() as conn:
                if cleanup_days is not None:
                    pl = ",".join("?" * len(override_ids))
                    not_in = f" AND source_id NOT IN ({pl})" if override_ids else ""
                    segs += [dict(r) for r in conn.execute(
                        "SELECT id, path, end_ts, source_id FROM segments"
                        f" WHERE end_ts IS NOT NULL AND end_ts < ?{not_in}",
                        [now - cleanup_days * 86400, *override_ids],
                    ).fetchall()]
                for sid in override_ids:
                    segs += [dict(r) for r in conn.execute(
                        "SELECT id, path, end_ts, source_id FROM segments"
                        " WHERE end_ts IS NOT NULL AND end_ts < ? AND source_id = ?",
                        [horizons[sid], sid],
                    ).fetchall()]
                if over_gb:
                    # Delete oldest segments until under limit
                    segs += [dict(r) for r in conn.execute(
                        "SELECT id, path, end_ts, source_id FROM segments"
                        " WHERE end_ts IS NOT NULL AND end_ts <"
                        " (SELECT AVG(end_ts) FROM segments WHERE end_ts IS NOT NULL)"
                    ).fetchall()]
            # A segment can match both the days and the GB pass.
            segs = list({s["id"]: s for s in segs}.values())

            # Notification horizon per source: the configured age horizon
            # where one exists; under GB pressure additionally the newest
            # end_ts deleted for that source (footage is gone either way).
            notif_cutoffs = dict(horizons)
            if cleanup_days is not None:
                global_cutoff = now - cleanup_days * 86400
                with video_db._connect() as conn:
                    for (sid,) in conn.execute(
                        "SELECT DISTINCT source_id FROM notification_events"
                    ).fetchall():
                        notif_cutoffs.setdefault(sid, global_cutoff)
            if over_gb:
                for seg in segs:
                    if seg.get("end_ts"):
                        sid = seg["source_id"]
                        notif_cutoffs[sid] = max(
                            notif_cutoffs.get(sid, 0.0), float(seg["end_ts"])
                        )

            result = delete_segments(video_db, video_dir, segs, notif_cutoffs)
            if result["deleted_segments"]:
                LOG.info(
                    "auto-cleanup: deleted %d segments, freed %.1f GB",
                    result["deleted_segments"], result["freed_bytes"] / 1e9,
                )
            if result["deleted_notifications"] or result["deleted_confirmations"]:
                LOG.info(
                    "auto-cleanup: deleted notifications=%d confirmations=%d",
                    result["deleted_notifications"],
                    result["deleted_confirmations"],
                )
        except Exception:
            LOG.exception("auto-cleanup error")

        stop_event.wait(3600)

    LOG.info("auto-cleanup loop stopped")


# ── Entry point ────────────────────────────────────────────────────────────────

def run(video_db_path: Path, video_dir: Path):
    if os.environ.get("WANYARD_LIVE_DETECTOR", "") == "1":
        from .live_detector import _configure_torch_threads
        _configure_torch_threads()

    from ultralytics import YOLO
    from .detection_settings import DetectionClassSelector
    from .video import VideoSegmentDB

    model_path = os.environ.get("YOLO_MODEL_PATH", "yolo11m.pt")
    LOG.info("loading YOLO model: %s", model_path)
    model = YOLO(model_path)

    video_db = VideoSegmentDB(video_db_path)
    with video_db._connect() as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            LOG.error("DB integrity check FAILED: %s — aborting", result)
            return
        LOG.info("DB integrity check passed")
    stop_event = threading.Event()
    predict_lock = threading.Lock()
    class_selector = DetectionClassSelector(video_db)

    def _shutdown(sig, frame):
        LOG.info("shutting down")
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    backfill_thread = threading.Thread(
        target=_backfill_loop,
        args=(model, video_db, video_dir, stop_event, predict_lock),
        daemon=True, name="backfill"
    )
    backfill_thread.start()

    # B4: the live detector (relay, subsecond) is now the authoritative live
    # pass. The old HLS-tag loop (YOLO on .ts fragments) is gone; backfill stays
    # gap-filler-only (scanned_at IS NULL). The recorder anchors closed archive
    # segments from their frame-0 SEI clock.

    live_detector_thread = None
    if os.environ.get("WANYARD_LIVE_DETECTOR", "") == "1":
        from .live_detector import start_live_detector
        live_detector_thread = start_live_detector(
            model, video_db, stop_event, predict_lock, class_selector
        )

    cleanup_thread = threading.Thread(
        target=_cleanup_loop,
        args=(video_db, video_dir, stop_event),
        daemon=True, name="cleanup"
    )
    cleanup_thread.start()

    srv = YoloSocketServer(
        SOCKET_PATH, model, video_db, video_dir, predict_lock, class_selector
    )
    srv._backfill_thread = backfill_thread
    srv._live_detector_thread = live_detector_thread
    srv.socket.settimeout(1.0)
    LOG.info("YOLO server listening on %s", SOCKET_PATH)

    while not stop_event.is_set():
        srv.handle_request()

    stop_event.set()
    srv.server_close()
    if Path(SOCKET_PATH).exists():
        Path(SOCKET_PATH).unlink()
    LOG.info("YOLO server stopped")
