# Wanyard

Website: https://wanyard.com

RTSP camera capture with BITC-stamped video, YOLO object detection, live HLS,
and a LAN web viewer.

## Quick start

```bash
git clone https://github.com/blowhacker/wanyard.git
cd wanyard
docker compose up --build -d
```

Open `http://localhost:8091/settings` to add cameras. YOLO model downloads automatically on first run.

For GPU acceleration, copy the override file:

```bash
cp docker-compose.gpu.yml docker-compose.override.yml
docker compose up --build -d
```

## What it does

- Burns BITC Unix time into each frame before recording and detection
- Records stamped camera streams as continuous MP4 segments
- Serves rolling live HLS streams for browser playback
- Runs live YOLO detection plus MP4 backfill, with detections keyed by BITC time
- Web UI: live view, timeline filmstrip, event feed with class filtering, clip export
- Auto-cleanup of old footage by age or disk usage

## Architecture

Main services in `docker-compose.yml`:

- **app** (`wanyard`) — web server, APIs, recording, HLS/MP4 serving
- **mediamtx** — RTSP relay
- **stamper** — burns BITC into frames and republishes stamped streams
- **yolo** (`wanyard-yolo`) — live detector, thumbnail crops, MP4 backfill

## Commands

```bash
wanyard serve        # web server, APIs, recording
wanyard stamp        # BITC stamper
wanyard yolo-serve   # YOLO live detection + MP4 backfill
```

## Web UI

- `http://localhost:8091` — timeline viewer with live streams and event feed
- `http://localhost:8091/settings` — add/remove cameras, system status, cleanup config
