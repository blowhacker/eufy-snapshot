#!/usr/bin/env python3
"""Validate media_time.read_frame in-container: status/shape smoke + YOLO recall.

Run inside the yolo container (has cv2 + ultralytics + model + /app/video):
    docker exec -i wanyard-staging-yolo python3 - < scripts/check_read_frame.py
"""
import os
import sqlite3
import time
from pathlib import Path

from wanyard import media_time as M

DB = "/app/video/video.db"
VID = Path("/app/video")
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
now = time.time()

# ── smoke: each provider/status ──────────────────────────────────────────────
rec = conn.execute(
    "SELECT s.source_id src, (s.actual_start_ts+vd.ts_offset) wt"
    " FROM video_detections vd JOIN segments s ON s.id=vd.segment_id"
    " WHERE vd.has_human=1 AND vd.ts_offset>=0 AND s.end_ts IS NOT NULL"
    "   AND s.actual_start_ts IS NOT NULL AND (s.actual_start_ts+vd.ts_offset)<?"
    " ORDER BY vd.id DESC LIMIT 1", (now - 1200,)).fetchone()
fr = M.read_frame(conn, VID, rec["src"], rec["wt"])
print("recorded :", fr.status, fr.provider, None if fr.frame is None else fr.frame.shape)
frl = M.read_frame(conn, VID, "tapo-front", now - 5)
print("live-5s  :", frl.status, frl.provider,
      None if frl.frame is None else frl.frame.shape, "retry_after", frl.retry_after)
frf = M.read_frame(conn, VID, "tapo-front", 9_999_999_999)
print("future   :", frf.status, frf.provider)

# ── recall: YOLO on read_frame output for recent person detections ───────────
from ultralytics import YOLO  # noqa: E402
model = YOLO(os.environ.get("YOLO_MODEL_PATH", "/app/models/yolo11m.pt"))
rows = conn.execute(
    "SELECT vd.id, s.source_id src, (s.actual_start_ts+vd.ts_offset) wt"
    " FROM video_detections vd JOIN segments s ON s.id=vd.segment_id"
    " WHERE vd.has_human=1 AND vd.ts_offset>=0 AND s.end_ts IS NOT NULL"
    "   AND s.actual_start_ts IS NOT NULL"
    " ORDER BY vd.id DESC LIMIT 15").fetchall()
ok = tot = 0
for row in rows:
    fr = M.read_frame(conn, VID, row["src"], row["wt"])
    if fr.frame is None:
        print(f"  det {row['id']}: {fr.status} (no frame)")
        continue
    tot += 1
    res = model.predict(fr.frame, classes=[0], conf=0.25, verbose=False)
    n = sum(len(r.boxes) for r in res)
    ok += 1 if n > 0 else 0
    print(f"  det {row['id']}: {fr.provider} persons={n}")
print(f"recall via read_frame: {ok}/{tot} person detections re-confirmed")
