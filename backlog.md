# Future Features

## Realtime live view
mediamtx->bitc ->to file only has subsecond latency

however:
new Date()/1000 - decodeLiveMarker(el.liveVideo)
10.736999988555908
new Date()/1000 - decodeLiveMarker(el.liveVideo)
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
- Defer automatic NVENC re-promotion. A source recording successfully on CPU
  fallback should not be disrupted automatically.


## Steal ideas
What can be stolen from this?
https://github.com/alebal123bal/khadas_yolov8n_multithread


## AI Query interface
Be able to ask questions like:
When did the rubbish bin get collected?


## HEVC playback on low-end Android — VIEWER only (low priority)
The live **wall** is fixed (it plays raw camera H.264 via go2rtc, any device).
The single-camera **viewer** still serves HEVC (recorded mp4 + the stamped live
HLS), so cheap Android Chrome (e.g. HMD) shows boxes but NO video there — its
Chrome has no HEVC decoder (desktop Chrome + iOS Safari do). Overlay/canvas
works because it's independent of video decode. The all-HEVC trial is the tax.

Need to confirm codec vs resolution on the device:
  MediaSource.isTypeSupported('video/mp4;codecs="hvc1.1.2.L150.90"')   // false = no HEVC
  MediaSource.isTypeSupported('video/mp4;codecs="avc1.640028"')        // true  = h264 ok
If hvc1 false + h264 true -> codec. If hvc1 true yet video fails -> 2304x1296
exceeds the cheap decoder -> need a lower-res rendition.

Options (trade-offs):
- A. Stamper back to H.264 — plays everywhere, lose ~30% HEVC storage win.
- B. Dual encode (HEVC store + H.264 for incompatible clients) — ~2x cost.
- C. Transcode-on-demand for HEVC-incapable UAs (clip download already
     H.264-transcodes) — CPU per stream + a live H.264 path.
- D. Accept: HEVC-capable clients only; document.


## Tests for the go2rtc-ingest re-architecture (low priority)
The ingest flip (camera → go2rtc → mediamtx → stamper) was verified by hand
(recording flowed, BITC anchor sane) but has no automated coverage. Lock in:
- BITC anchor accuracy survives the extra go2rtc hop (media_epoch decoded from
  pixels still matches, recorded ts resolves within frame tolerance).
- gen-mediamtx sources from `rtsp://go2rtc:8554/<id>` (not the camera).
- gen-go2rtc reads each camera's configured URL directly + emits `stun:<port>`
  candidates (no hostname dependency).
- Wall WHEP proxy → go2rtc; HLS-first → WebRTC-swap fallback path.
See docs/media-time-architecture.md "Tests" for the existing BITC regressions.
