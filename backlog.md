# Future Features

## Object permanence

idea needs research. yolo just tags individual frames


## Recorder robustness: remaining supervisory work

Delivered on 2026-08-13: persistent archive RTSP subscriptions across MP4
boundaries, isolated HLS, fully drained FFmpeg pipes, video-progress watchdogs,
source-local reconnect with capped exponential backoff, per-source status,
readable-file validation, exact archive/detection reconciliation, and real
FFmpeg/MediaMTX rotation/outage tests.

Still worth adding after production observation:

- Add jitter, cooldown, and a maximum recovery rate for long persistent
  outages, plus generation counters that prove only one FFmpeg generation owns
  a source.
- Expose last progress time, restart reason/result, next retry, and cooldown in
  the status UI; distinguish an offline camera from an unhealthy app.
- Extend the fault suite with a publisher that stays connected but stops video,
  duplicate-generation assertions, and multi-hour outage/soak coverage.
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


## Recording search: semantic phase

The evidence-backed search POC exists and answers constrained class/time/camera
questions from indexed detections. The next phase is semantic interpretation
and memory: natural concepts such as “the bins moved” or “the black cat”,
identity/attribute persistence across events, uncertainty in the answer, and
links to every supporting clip. Language-model output must remain a query plan
and summary over retained evidence, never become the evidence itself.
