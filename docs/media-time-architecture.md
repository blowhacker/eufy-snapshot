# Media-Time Architecture

Status: accepted; SEI frame clock active since 2026-07-07
Owner: video pipeline

## Rule

Wanyard has one public time: the frame clock — Unix seconds carried on each
frame as an H.264 SEI NAL.

Any timestamp that leaves a media boundary must be clock time: detection
`abs_ts`, event time, notification time, URL `ts`, overlays, API filters, and
activity thumbnails all use the same coordinate.

Container clocks, filenames, `start_ts`, HLS program date time, file mtimes,
player `currentTime`, RTP, and ffmpeg offsets are not public time. They can
order media or seek inside media, but they must not become truth.

## Coordinates

- Clock time: `(source_id, t)` where `t` is Unix seconds read from the frame's
  SEI. This is the only cross-module absolute time.
- Media time: `(asset, offset)` where `offset` is seconds inside one MP4 or HLS
  fragment. This is private to media access code.

The conversion is always:

```text
media_offset = clock_ts - media_epoch
clock_ts = media_epoch + media_offset
```

`media_epoch` means "the clock time at media offset zero" for that asset. If the
epoch is unknown, the answer is `no_anchor`; there is no clock fallback.

## Carrier

The clock rides as an unregistered user-data SEI NAL inside each access unit
(`src/wanyard/sei.py`): 38-bit Unix-centisecond payload + CRC8, bound
structurally to its own frame. `sei_copy` stamping injects it packet-level with
no re-encode (zero generation loss, archive = camera bits); the `reencode`
fallback for non-H.264 input emits the same SEI on its encoded packets. Trust
or discard: a wrong UUID or bad CRC reads as absent, never as a wrong time.

## Boundary

`src/wanyard/media_time.py` owns clock-to-media conversion.

- `resolve(...)` chooses recorded MP4, live HLS, or no media for a clock time.
- `read_frame(...)` returns the frame for a clock time, using the same mapping.

Callers pass `(source_id, clock_ts)`. They do not subtract `start_ts`, inspect
HLS program date time, or calculate offsets themselves.

## Anchors

- MP4 segments store `segments.media_epoch`, decoded from the video's own SEI.
  Closed MP4 is authoritative for past time.
- Live HLS has only relative fragment offsets. Its absolute window is anchored
  by reading the SEI clock from a fragment frame.
- HLS program date time is transport metadata only. It is never scene time.

Provider choice is ordinary:

- closed recorded segment covers `t` -> MP4
- current rolling live window covers `t` -> HLS
- neither covers `t` -> gap/no media
- candidate media has no decoded anchor -> `no_anchor`

## Data Contract

- `video_detections.abs_ts` is clock time.
- Event IDs and notification targets identify clock time, not storage offsets.
- `media_epoch` and `media_offset` are internal mapping metadata.
- `start_ts` remains recorder bookkeeping for filenames/order only.
- Storage tags such as provisional/open/closed event IDs stay server-side.

## Browser

The browser displays clock time from server APIs and asks the server for nearby
detections around that clock value. The player reads the same SEI clock from the
finalized MP4 (the frame clock) for frame-exact seeking; where a file has no
frame clock it maps through the segment's server-anchored `media_epoch`. Live
playback may use video `currentTime` to advance on screen, but public queries
still use clock time.

Recent activity thumbnails use event IDs. The server resolves those IDs back to
clock time and chooses live HLS or recorded MP4 through the same media boundary.

## Live wall (outside the boundary)

The live wall is a pure live-preview path and deliberately sits **outside** the
clock boundary. It plays the raw camera (via go2rtc — WebRTC, with an instant
LL-HLS first layer), which carries **no SEI clock** and **no public time**: no
detections, no overlays, no `ts`. It never produces scene time, so it doesn't
violate the one-time rule. Clicking a tile hands off to the normal single-camera
viewer, which resolves everything through this boundary as usual.

go2rtc being the camera ingest (`camera → go2rtc → mediamtx → stamper`) is a
transport change only — recording/clock anchoring downstream is unchanged; the
stamper attaches world time as SEI and `media_epoch` is still decoded from the
frame's own clock.

## Tests

Keep these regressions covered:

- A stored detection resolves back to its own media offset within frame
  tolerance.
- A closed MP4 segment wins over live media for past time.
- Live HLS is anchored by the decoded frame clock, not transport date metadata.
- Frame reads for HLS and MP4 return the frame nearest the requested clock time.
- Missing anchors return `no_anchor`, not guessed offsets.
- Event links, notification links, thumbs, overlays, and live activity filters
  all use the same clock timestamp.
