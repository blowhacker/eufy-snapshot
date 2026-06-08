#!/bin/sh
# Container healthcheck: verifies web server, recording threads, and HLS freshness

python3 -c "
import urllib.request, json, sys, os, time
from pathlib import Path
from wanyard.config import load_config

config_path = os.environ.get('WANYARD_CONFIG', '/app/config.yaml')
try:
    port = int(os.environ.get('WANYARD_HEALTH_PORT') or load_config(config_path).web.port)
except Exception:
    port = 8091

# Basic web check + thread health
try:
    r = urllib.request.urlopen(f'http://localhost:{port}/api/settings/status', timeout=5)
    d = json.load(r)
except Exception as e:
    print('status check failed:', e)
    sys.exit(1)

threads = d.get('recording_threads', {})
dead = [k for k, v in threads.items() if not v]
if dead:
    print('dead recording threads:', dead)
    sys.exit(1)

# HLS freshness: m3u8 must be <60s old per active camera
video_dir = Path(os.environ.get('VIDEO_DIR', '/app/video'))
live_dir = video_dir / 'live'
if live_dir.exists():
    for cam_dir in live_dir.iterdir():
        m3u8 = cam_dir / 'live.m3u8'
        if m3u8.exists():
            age = time.time() - m3u8.stat().st_mtime
            if age > 60:
                print(f'HLS stale for {cam_dir.name}: {age:.0f}s old')
                sys.exit(1)

print('ok')
sys.exit(0)
"
