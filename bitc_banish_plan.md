# Banishing BITC and its acolytes

Status: **Phase 2 done (2026-07-07)** — gate cleared (pre-flip pixel archives
purged), then the readers were killed: no consumer decodes pixels, `bitc.py`
is deleted (value codec folded into `sei.py`), the viewer's per-frame canvas
decode is gone, and the acolytes are renamed to clock/SEI vocabulary. Clock
chains are `sei -> mapped`. Full test suite green (102 tests, av/ffmpeg E2E
included). Branch: `bitc-banish-p2`.

Phase 1 done (2026-07-05) — no process writes pixels; the reencode fallback
injects SEI on its encoded packets, output is h264-only, the recorder's hvc1
dance and recording_quality.py are gone. Prod runs SEI (`sei_copy` default) on
master since 2026-07-05.

## The constraint that shapes everything

Pixel-marked archives still exist: everything recorded before the 2026-07-05
flips. Global retention is 5 days, so by ~2026-07-10 no pixel-marked file
remains. Reader fallbacks must outlive the files they read; the producer can
die immediately. Hence two phases with a hard gate between them.

## Phase 1 — kill the producer (can run now)

The only remaining pixel-writer is the stamper's reencode fallback
(`render_yuv420`). Convert it to emit the same clock as everyone else instead
of deleting it (it stays as the safety net for non-h264 cameras):

1. Reencode path injects SEI into its encoded packets instead of burning
   pixels — `sei.inject()` on encoder output before mux (~20 lines). The
   whole repo becomes single-clock even through the fallback.
2. Fallback encoder becomes h264-only (`h264_nvenc` -> `libx264`; drop
   `hevc_nvenc` from auto-resolve). The injector is H.264-only, HEVC's only
   justification was re-encode efficiency, and this kills a remnant family
   for free: the recorder's `_stamped_codec` probe, the hvc1/hev1 tag dance
   and its retry-on-mismatch path, `native_hls` HEVC handling.
3. Remove `bitc.render*` / producer-side `mask` from the stamper; the
   "visible burned timecode" mode dies.
4. Cull dead machinery: `write_stamper_reload_request` lost its only caller
   when the quality API went — the supervisor's `_handle_reload_request` +
   `stamper-reload.json` go with it. `recording_quality.py` is deleted once
   the fallback uses the stamper's plain env knobs (`self.cq/crf/maxrate`).

After Phase 1: no process writes pixels; `bitc.py` is read-only legacy support.

## Gate — before Phase 2

On prod, expect zero:

    SELECT COUNT(*) FROM segments WHERE start_ts < <flip_ts>;

(video db lives at /app/video/video.db). Plus a spot check that no .mp4 older
than the flip survives on disk. Roughly 2026-07-10 given 5-day retention.
Re-run the gate in the Phase 2 branch deploy as a guard.

## Phase 2 — kill the readers (after the gate)

1. Remove the pixel fallback branch from the six consumers:
   - `live_detector` (+ its `bitc.mask` call)
   - `video._decode_bitc_media_epoch`
   - `media_time._decode_live_bitc_probe`
   - `video._yolo_tag_video` (KEEP its plausibility clamp — it guards SEI too)
   - `web._extract_live_thumb`
   - `video2.js` browser-side pixel machinery: `decodeLiveMarker`,
     `#bitcEpoch` per-frame canvas decode, and the wall-time guard that
     existed because of pixel false-CRCs.
   Clock-source fallback chains shrink to `sei -> mapped`.
2. Fold the value codec into `sei.py` (`encode_value`, `_crc8`, `_MAX_VALUE`)
   and delete `bitc.py`.
3. Tests: delete `test_bitc.py` and `test_live_detector_marker.py`; prune
   marker cases from `test_stamper`, `test_video_bitc_anchor`,
   `test_recorder_fault_integration`; pixel-fallback tests in `test_sei` /
   `test_video_bitc_anchor` are deleted with the fallbacks they cover.
4. Rename the acolytes (code-only; DB schema untouched — `media_epoch` is
   already clock-neutral): `_BitcMediaTimeline` -> `_MediaTimeline`,
   `_decode_bitc_media_epoch` -> `_decode_media_epoch`, `BITC-ANCHOR` log
   tags -> `CLOCK-ANCHOR`, `bitcEpoch`/`bitcTimeOffset` in video2.js, `bitc`
   references in `cli.py` / wall comments. Rewrite
   `docs/media-time-architecture.md` + README around "SEI frame clock";
   close backlog entries.

## What deliberately survives

- The one-clock architecture — only the carrier changes; "time travels with
  the media" lives.
- `reencode` stamp mode (now SEI-emitting) as the non-h264 safety net.
- The strip-covering `tile-bar` on the wall (good UI regardless; comment
  cleanup only).
- `sei_copy`/`reencode` DB/env knobs.

## Sequencing

Phase 1: normal branch -> tests -> deploy. Phase 2: own branch after the gate
query returns zero, mechanical deletion across six consumers + viewer.
