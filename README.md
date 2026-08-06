# Wanyard

Website: https://wanyard.com

RTSP camera capture with an SEI frame clock stamped onto every frame, YOLO
object detection, live HLS, and a LAN web viewer.

## Quick start

```bash
git clone https://github.com/blowhacker/wanyard.git
cd wanyard
docker compose up --build -d
```

Open `http://localhost:8091/settings` to add cameras. YOLO model downloads automatically on first run.
Docker installs the exact Python package set in `requirements.lock`; dependency
updates should change that file deliberately and pass the full test suite.

For GPU acceleration, copy the override file:

```bash
cp docker-compose.gpu.yml docker-compose.override.yml
docker compose up --build -d
```

## What it does

- Stamps Unix time onto each frame as an H.264 SEI clock before recording and
  detection (codec copy — zero generation loss, archive = camera bits)
- Records stamped camera streams as continuous MP4 segments
- Serves rolling live HLS streams for browser playback
- Runs live YOLO detection plus MP4 backfill, with detections keyed by clock time
- **Live wall (god view)** — all cameras at once on the landing page, instant
  load with near-zero-latency WebRTC; click a camera to open its full viewer
- Web UI: live view, timeline filmstrip, event feed with class filtering, clip export
- Auto-cleanup of old footage by age or disk usage

## Architecture

Ingest: `camera → go2rtc → mediamtx → stamper → recorder/yolo`. go2rtc is the
single camera puller; it serves the live wall over WebRTC directly (instant,
low latency) while mediamtx sources from it for recording/stamping/HLS — no
extra camera pull.

Main services in `docker-compose.yml`:

- **app** (`wanyard`) — web server, APIs, recording, HLS/MP4 serving
- **go2rtc** — camera ingest; serves the live wall over WebRTC (WHEP)
- **mediamtx** — RTSP relay (sources from go2rtc), live HLS
- **stamper** — attaches the SEI frame clock and republishes stamped streams
- **yolo** (`wanyard-yolo`) — live detector, thumbnail crops, MP4 backfill

## Commands

```bash
wanyard serve        # web server, APIs, recording
wanyard stamp        # SEI frame-clock stamper
wanyard yolo-serve   # YOLO live detection + MP4 backfill
wanyard stereo-inspect tapo-front garden-old  # geometry + moving-vehicle timing report
```

`stereo-inspect` reads recorded frames and detections only. Its default
three-hour timing window looks for up to 30 shared moving vehicles, estimates
the right-camera clock offset at 10 ms resolution, and writes a confidence
gate, offset plot, and paired-vehicle montage alongside the geometry report.

## Tests

Run the fast unit suite:

```bash
python3 -m unittest discover -s tests -v
```

Run the isolated recorder fault suite with real FFmpeg and MediaMTX processes:

```bash
./scripts/test-recorder-faults.sh
```

The fault suite uses a separate Compose project, synthetic RTSP publishers,
temporary databases, and temporary video directories. It does not read camera
configuration or production footage.

## Web UI

- `http://localhost:8091` — live wall (all cameras); click one for its viewer
- `http://localhost:8091/detections` — object-tag filters and detection thumbnails grouped by camera
- `http://localhost:8091/?source=<id>&live=1` — single-camera timeline viewer + event feed
- `http://localhost:8091/settings` — cameras, detection classes, media health history, notifications, and storage
