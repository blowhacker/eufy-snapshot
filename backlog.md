# Future Features

## Realtime live view
mediamtx->stamper ->to file only has subsecond latency

however, the browser-visible live edge lagged ~10s (measured before the
SEI cutover via the now-retired pixel decoder):
new Date()/1000 - liveTailCurrentTs()
10.736999988555908
new Date()/1000 - liveTailCurrentTs()
10.817000150680542

- quick hacky win is to tune hls
- proper fix is WebRTC

> need to weigh compatibility across devices - iphone? different browsers

> WebRTC iirc only does H264 so storage costs explode i'd think



## Object permanence

idea needs research. yolo just tags individual frames


## Recorder robustness Phase 2: source-local automatic recovery

Phase 1 makes codec changes survivable and exposes per-source progress/failure
telemetry. After observing it in production for several days, add automatic
recovery based on recording progress rather than thread liveness.

- Track the last successful segment and HLS update for each source.
- Treat repeated failures or prolonged no-progress as a stalled source.
- Reconstruct only the failed source's `VideoWorker`; do not restart the app,
  stamper, or other camera workers.
- Terminate, wait for, and reap the old FFmpeg process before replacing a
  worker. Enforce one active worker generation per source.
- Use consecutive-failure thresholds, exponential backoff, jitter, cooldown,
  and a maximum recovery rate to prevent restart storms.
- Expose recovery generation, attempts, reason, last result, and cooldown
  through the status API.
- Keep camera degradation separate from container/process health.
- Extend the isolated recorder fault suite with stalled-but-live workers,
  duplicate-worker prevention, restart-rate limits, and persistent outage
  scenarios.
- Add automatic NVENC re-promotion after a long cooldown. While a source is on
  CPU fallback, run a non-disruptive open+encode probe (initially after 30
  minutes). Switch that source back only after the probe succeeds; if the real
  stream open still fails, roll back immediately to the known-good CPU encoder.
  Use exponential cooldown and expose the active encoder, last probe result,
  next retry time, and promotion/rollback count. This belongs to the stamper,
  not recorder segment rotation.


## Steal ideas
What can be stolen from this?
https://github.com/alebal123bal/khadas_yolov8n_multithread


## AI Query interface
Be able to ask questions like:
When did the rubbish bin get collected?


## Tests for the go2rtc-ingest re-architecture (low priority)
The ingest flip (camera → go2rtc → mediamtx → stamper) was verified by hand
(recording flowed, clock anchor sane) but has no automated coverage. Lock in:
- Clock anchor accuracy survives the extra go2rtc hop (media_epoch decoded from
  the frame's SEI still matches, recorded ts resolves within frame tolerance).
- gen-mediamtx sources from `rtsp://go2rtc:8554/<id>` (not the camera).
- gen-go2rtc reads each camera's configured URL directly + emits `stun:<port>`
  candidates (no hostname dependency).
- Wall WHEP proxy → go2rtc; HLS-first → WebRTC-swap fallback path.
See docs/media-time-architecture.md "Tests" for the existing frame-clock regressions.
