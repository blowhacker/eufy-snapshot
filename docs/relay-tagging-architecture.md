# Relay single-pass: video + tagging architecture, and where the time-gap enters

Branch `relay-single-pass`. This documents the ACTUAL flow as built (file:line
refs to current code), then isolates exactly where the overlay time-anchor gap
("clairvoyant boxes") enters. Written 2026-06-13 to ground the anchor fix.

---

## 1. Components & connections

```
                         ┌──────────────────────────────────────────────┐
  camera ──1 RTSP pull──▶│  mediamtx relay  (sourceOnDemand, -c copy)     │
 (H264 + G711,           │  re-bases RTP to ~0 PER CONSUMER SESSION       │
  ~100ms+ internal       └──────────────┬───────────────┬────────────────┘
  capture→encode lag)                   │ fan-out (free, frame-identical)
                                        │               │
                    ┌───────────────────▼──┐      ┌──────▼─────────────────┐
                    │ RECORDER (ffmpeg)     │      │ DETECTOR (PyAV, yolo)  │
                    │ video.py CaptureWorker│      │ live_detector.py        │
                    │  → MP4 archive         │      │  → video_detections     │
                    │  → HLS live (.ts+PDT)  │      │  (abs_ts BITC time)     │
                    └───────────┬───────────┘      └──────────┬─────────────┘
                                │                             │
                                ▼                             ▼
                         segments table              video_detections table
                         (media_epoch = THE anchor)  (abs_ts = BITC time)
                                │                             │
                                └──────────────┬──────────────┘
                                               ▼
                              web.py /api/video/*  +  static/video2.js
                              player: currentTs = media_epoch + currentTime
                              overlay boxes drawn at (abs_ts − media_epoch)
```

Recorder + detector are **separate consumers** of the relay. The relay's single
camera pull is fanned out; each consumer opens its own RTSP session.

---

## 2. Video flow (camera → relay → consumers)

- Camera emits H264 + G711 over RTSP. Internal sensor→encode latency (~100ms+)
  is **unobservable from our side** — the "event horizon".
- mediamtx (`sourceOnDemand: yes`) pulls the camera only while a consumer is
  attached, copies packets to every consumer **byte-identical** (one encode).
- **mediamtx re-bases RTP per consumer session** (measured 2026-06-13: a fresh
  PyAV consumer's first `pkt.pts` → `rtp_sec ≈ 0.2`, not an absolute camera
  clock). So each consumer's RTP starts near 0 *at the wall-clock instant it
  connected*. Two consumers that connect at different times have **different RTP
  bases** — there is no shared absolute RTP clock across recorder and detector.

---

## 3. Recorder (`video.py`, `CaptureWorker`)

ffmpeg command (`_start_segment`, ~3263):

```
ffmpeg -use_wallclock_as_timestamps 1 -rtsp_transport tcp -i <relay url>
   -c:v copy -c:a aac -movflags +faststart  <YYYY-MM-DD_HH-MM-SS.mp4>      # archive
   -c:v copy -c:a aac -f hls -hls_time 2 -hls_list_size N
   -hls_flags ...+program_date_time -hls_segment_filename seg_%010d.ts  live.m3u8  # live
```

- One ffmpeg, two outputs: the **MP4 archive** (`-c copy`, faststart → moov at
  close) and the **rolling HLS** (2s `.ts` fragments + `#EXT-X-PROGRAM-DATE-TIME`).
- **`-use_wallclock_as_timestamps 1`**: ffmpeg discards camera RTP and stamps
  each packet's pts with **ffmpeg's own NTP wallclock at packet receipt**,
  normalized so frame 0 ≈ container-offset 0. So:
  - **container offset of frame f = W_rec(f) − W_rec(0)**, where W_rec = the
    recorder's arrival wallclock.
  - the HLS **PDT of a fragment = the wallclock pts of its first frame = W_rec**
    of that frame.
- Segments rotate every `_MAX_SEGMENT_SECONDS` (~10min); a short/early-exit
  segment backs off (`run`, ~3210). On rotation `_stop_segment` seals the MP4.
- The absolute `W_rec(0)` is **not stored in the container** (pts normalized to
  ~0). It survives only in the first fragment's PDT.

---

## 4. Detector (`live_detector.py`)

- One PyAV thread per camera off the relay (`rtsp://mediamtx:8554/<src>`),
  reader/worker split (reader demuxes+stamps+decodes+parks freshest frame;
  worker runs rate-capped YOLO at `WANYARD_LIVE_FPS`=2).
- Per frame: `rtp = pkt.pts * time_base` (session-relative), `wall = time.time()`
  at packet receipt = **W_det**.
- **Rolling-slew anchor** (`_TimeAnchor`): `delta = min(W_det − rtp)` over a 10s
  ANCHORING window, then slewed toward a rolling-90s-window min at ≤0.002 s/s.
  Stamp **`abs_ts = rtp + delta`**.
  - Since `delta ≈ min(W_det − rtp)`, `abs_ts ≈ W_det` for the least-delayed
    frame and the denoised arrival-wall floor for the rest. **`abs_ts` is a
    jitter-cleaned detector arrival wall** (≈ capture + min network latency).
  - Re-anchors on RTP discontinuity (>±tolerances); per-connection (never reuse
    delta across reconnect — base is session-relative).
- Writes `video_detections(segment_id, source_id, abs_ts, has_human, confidence,
  boxes_json, classes_json)`. Resolves the open segment; if `media_epoch` not yet
  set, **queues** rows and flushes when it appears (chicken-egg guard).
- **Claim**: when a fully-covered segment closes, sets `segments.scanned_at` so
  backfill skips it. Partial coverage → left for backfill.
- **Frametimes dump**: appends `(abs_ts, wall)` per frame to
  `live/<src>/frametimes.jsonl` (ring) — consumed by the recorder's anchor step.

---

## 5. Tagging storage & backfill

- `video_detections` = the single detection store (live detector authoritative
  after B4). `abs_ts` is BITC time; box coords normalized 0–1.
- `backfill_loop` (`yolo_server.py`) YOLO-tags only **unclaimed** closed segments
  (`scanned_at IS NULL AND media_epoch IS NOT NULL`) at 1fps — gap-filler only.
  Undecodable MP4s now get `mark_scanned` (no tight-spin). Then `extract_events`.
- HLS-tag loop (the old second YOLO pass) and the `hls_events` table are retired
  (B4 removed writers/readers; table dropped in B5).

---

## 6. `media_epoch` lifecycle — THE anchor (one value per segment)

`media_epoch` = world (NTP) time of the MP4's container-offset 0. It is set in
TWO phases:

**(a) While the segment is OPEN** — `run()` calls `_observe_live_anchor()` every
5s (~3225). It reads the HLS playlist, takes `min(PDT candidates)` and sets
`media_epoch = min(PDT)` (`set_segment_media_start`, keep-earliest). This is the
**provisional** anchor the live overlay uses during the open 10-min window.

**(b) At CLOSE** — `_stop_segment` (~3296):
1. `_observe_live_anchor(seg_id, seg_start)` once more (PDT).
2. `_align_media_epoch(pts, det_frametimes, seg_start, arrival_prior=PDT)`
   (jitter-fingerprint): pairs MP4 container pts ↔ detector frametimes by
   common-mode delivery-jitter pattern, then `media_epoch = median(abs_ts − pts)`
   over paired frames. On success → `set_media_epoch_absolute` (**overwrites**).
3. FALLBACK if alignment refuses → keep PDT and subtract container `v_start`
   (`correct_media_epoch_axis`).

So a closed segment's final anchor = the jitter-fingerprint value (or the
PDF−v_start fallback). ANCHOR-ALIGN / ANCHOR-PDT / ANCHOR-AUDIT log lines record
each.

---

## 7. Player / overlay (`web.py`, `video2.js`)

- `/api/video/resolve` returns the segment's `media_epoch`. Player maps
  `currentTs = media_epoch + video.currentTime`, and recorded playback is the
  direct MP4 (no rewrap).
- Overlay boxes (recorded): `/api/video/overlays` → `detections_between` →
  `video_detections` rows (abs_ts, boxes). `overlayTracklets` chains them into
  short tracklets; a box for a detection at `abs_ts` is drawn at video time
  `abs_ts − media_epoch`.
- Live overlay (`live=1`): `/api/video/live` → `live_status.recent_detections`
  = last 30s of `video_detections`, matched to `hls.js playingDate`.

**Enclosing condition:** the box for a detection drawn at `abs_ts − media_epoch`
lands on the video frame the player shows at that `currentTime`. That frame's
container offset is `abs_ts − media_epoch`. It encloses iff that frame is the
same physical frame the detector saw when it stamped `abs_ts`.

---

## 8. THE TIME AXES — where the gap enters

Every clock in play, and how they relate:

| symbol | meaning | properties |
|---|---|---|
| `C(f)` | true camera capture time of frame f | unobservable (event horizon, ~100ms+) |
| `R_rec(f)`, `R_det(f)` | RTP of frame f at each consumer | **session-relative** (different base per consumer); camera clock, drifts −88ppm |
| `W_rec(f)` | recorder arrival wallclock | NTP; container offset = `W_rec(f) − W_rec(0)`; HLS PDT = `W_rec(f)` |
| `W_det(f)` | detector arrival wallclock | NTP; `abs_ts(f) ≈ W_det(f)` (jitter-cleaned) |

**The identity we need.** Player frame at offset `o` has `o = W_rec(f) − W_rec(0)`.
Box for that frame drawn at `currentTime = abs_ts(f) − media_epoch ≈ W_det(f) −
media_epoch`. Enclose ⇒ these equal ⇒

```
   media_epoch = W_det(f) − W_rec(f) + W_rec(0)
```

If recorder and detector receive the SAME frame at the same wall
(`W_det(f) ≈ W_rec(f)`, B0 lab: 0.2ms), this collapses to the clean target:

```
   media_epoch  =  W_rec(0)   =  recorder's wallclock at frame 0
```

**So the entire anchor problem = recover `W_rec(0)` (recorder frame-0 arrival
wall) accurately.** Everything else cancels by construction.

### Where each method goes wrong

- **Raw HLS PDT of fragment 0** *is* literally `W_rec(0)` by definition — yet it
  measures **~1.7s too large** (leads). ⇒ either ffmpeg's
  `-use_wallclock_as_timestamps` does NOT stamp at true receipt (buffered/burst
  delivery at connect compresses the first frames' wall stamps), or PDT is
  written from a later wall than frame-0 receipt. **This is the key unknown to
  measure.**
- **PDT − v_start**: tries to remove audio-preroll container offset; `v_start` is
  AAC-transcode fiction (0–2.2s) ⇒ wobbles.
- **Jitter-fingerprint** (current): computes `median(W_det(paired) −
  container_pts(paired)) = median(W_det − W_rec) + W_rec(0)`. If the pairing is
  frame-exact, `W_det − W_rec ≈ 0` ⇒ correct. **But** per-frame walls of
  adjacent frames overlap (jitter sd 85ms > frame interval 50–66ms), so the
  pattern can lock **shifted by k frames** ⇒ `media_epoch` off by `k·interval`.
  Seg 1479: locked ~1s off at **98% reported confidence** (false confidence).
  Intermittent (most segments enclose; garden walk passed).
- **Shared RTP clock**: would be frame-exact, but **impossible** — mediamtx
  re-bases RTP per session (§2), so `R_rec` and `R_det` have different bases.

### The gap, stated plainly

The overlay leads when `media_epoch` is **too large** (frame shown for a given
`abs_ts` is too early → box sits ahead of the subject). Every candidate method is
really trying to estimate `W_rec(0)`, and each fails differently: PDT
over-estimates by a possibly-stable ~1.7s; the fingerprint occasionally
mis-pairs by ~1s with false confidence; v_start injects fiction; RTP-sharing is
impossible.

---

## 9. The investigation this enables (next session)

1. **Measure what `-use_wallclock_as_timestamps` / PDT actually stamp** vs true
   frame-0 receipt — is the ~1.7s PDT lead a STABLE constant (handshake + demux
   burst)? If `media_epoch = PDT − C` with measured constant `C` holds across
   many segments and both cameras, that is the simplest recorder-native anchor.
2. **Establish method-independent ground truth**: single-subject (garden)
   position-match calibration → the `media_epoch` that actually encloses, per
   segment. We currently have no trusted reference.
3. **Pick + gate**: whichever estimator of `W_rec(0)` is robust, wrap it in a
   confidence gate that REFUSES low-trust locks (seg-1479 lesson: never trust a
   single self-consistent score; cross-check against an independent prior, and
   fall back / flag rather than guess).

Tool: position-match (YOLO on `ffmpeg -ss` frames vs detector box centers) —
clean on single-subject scenes, noisy on the multi-person front street.
