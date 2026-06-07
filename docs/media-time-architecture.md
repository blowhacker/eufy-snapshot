# ADR: Media-time architecture

Status: accepted (2026-06-07)
Owner: video pipeline

## Problem

A single timestamp does not mean a single point in video. The MP4 seek path
subtracts `start_ts`; YOLO frame reads and notification candidates subtract
`actual_start_ts`; thumbnail nav deliberately re-adds `start_ts` so the offset
math cancels. These bases disagree by *drift* (median ~0.64s on prod, 13% >1s,
max 4.1s), so a notification click lands ~0.6s off the frame it shows while the
event grid lands exact. The world↔media conversion is done ad hoc in ~6 places
with inconsistent bases. That is the leak.

Root cause: cameras give no usable wall clock (RTP is a relative 90kHz media
clock; RTCP is unreliable on cheap cams), so we restamp with host wall clock
(`-use_wallclock_as_timestamps 1`). The MP4 muxer then normalizes first PTS to
~0, losing the absolute mapping inside the file. HLS keeps it via
`EXT-X-PROGRAM-DATE-TIME` (PDT). So the only honest stored fact is
`(asset, offset)`; every wall-clock value is a *derived projection* needing a
base — and we picked the base inconsistently.

## Decision

There are two coordinate spaces. Exactly one component crosses between them.

1. **World time** — `(source_id, t)`, `t` = unix UTC seconds. The only
   coordinate that crosses module boundaries. User-facing.
2. **Media time** — `(asset, offset)`, `offset` = seconds into a specific file
   or stream. Exists only inside the resolver and the player/decoder.

`start_ts`, `actual_start_ts`, `ts_offset`, PDT are **not timestamps**. They are
storage facts used to build the mapping. Only the resolver reads them.

## Vocabulary

| Term | Meaning | Replaces |
|------|---------|----------|
| `t` (world ts) | unix UTC seconds; best-known wall instant of a frame | the overloaded "abs_ts"/"ts" |
| `media_offset` | seconds into an asset | `ts_offset` |
| `media_epoch` | world time of `media_offset == 0` for an asset | `actual_start_ts` |
| `nominal_open_ts` | recorder's pre-launch clock; filename/ordering key, **not** a coordinate | `start_ts` (as a time) |
| anchor | `{provider, asset_ref, media_epoch, duration}` | scattered offset math |
| provider | `mp4` (recorded) \| `hls` (live) \| `none` (gap) | `h:`/`d:`/`o:` leak |

## Invariant (the property that proves the abstraction holds)

```
world→media(t)  = t - media_epoch        # clamp [0, duration]
media→world(o)  = media_epoch + o

For every stored detection d:
  loc = resolve(d.source_id, media→world(d.asset, d.media_offset))
  assert loc.provider     == d.asset.provider
  assert |loc.media_offset - d.media_offset| < EPS    # round-trip identity
```

If `start_ts` (now `nominal_open_ts`) ever creeps back in as a coordinate, this
fails. Enforced in CI (py) and on-device (js). `EPS = 0.05s`.

## Resolver contract

```
resolve(source_id: str, t: float) -> MediaLocation

MediaLocation {
    provider:     "mp4" | "hls" | "none"
    url:          str | None       # what the player/decoder opens
    media_offset: float | None     # = t - anchor.media_epoch, computed ONCE
    coverage:     {start: float, end: float} | None  # world-span covered
    anchor:       Anchor | None     # for media→world on the way back
    reason:       str               # "recorded" | "live" | "gap" | "no_anchor"
}
```

Rules:
- Resolver is the **only** place `t - <base>` is computed. No caller does offset
  math.
- Provider chosen from `t` vs now + segment table, not from caller intent.
- Gap (no segment, no live) → `provider:"none"` + nearest `coverage`. Callers
  render gaps uniformly.
- **`media_epoch` unknown → `provider:"none"`, `reason:"no_anchor"`. Never fall
  back to `nominal_open_ts`.** Exposes real unknowns instead of papering over
  them with a wrong base.

## Migration plan (each step shippable, each guarded by the round-trip assert)

1. **Design doc** (this file).
2. **Resolver types + API** in a new `media_time.py` using the new vocabulary;
   no callers moved yet. Pure functions over a connection.
3. **Rename/alias columns** with compatibility views/helpers so existing callers
   keep working until moved (`actual_start_ts`→`media_epoch`,
   `ts_offset`→`media_offset`, `start_ts`→`nominal_open_ts`).
4. **Authoritative `media_epoch`**: writer ties it to its own ffmpeg's first
   `.ts` PDT (1:1, not `MIN` over a fuzzy shared-dir window). Unknown → NULL,
   surfaced as `no_anchor`. Backfill.
5. **Backend callers → resolver** (yolo frame read, notification candidates;
   already on the correct base — log round-trip ε to confirm zero regression).
6. **Frontend seek → resolver**: UI calls `/api/video/resolve?source=&ts=`;
   `V2Player.seek(source_id, t)` only. No `start_ts` in JS. Live (`hls`) seek via
   hls.js fragment `.programDateTime`, killing the `wallClockOffset` fudge.
7. **Normalize event/notification contract**:
   `{source_id, t, duration, class, state, thumb_url, target_url}`. Storage refs
   (`h:`/`d:`/`o:`) stay server-side.

## Test cases

- **Round-trip identity** (the invariant): every detection resolves back to its
  own asset+offset within EPS. Both py and js.
- **Base correctness**: detection at known `media_epoch + o` resolves to
  `media_offset == o`, independent of `nominal_open_ts` drift.
- **Provider selection**: `t` inside a closed segment → `mp4`; `t` in the live
  window → `hls`; `t` in neither → `none` with nearest coverage.
- **No-anchor honesty**: segment with NULL `media_epoch` → `provider:"none"`,
  `reason:"no_anchor"` — never a `nominal_open_ts` guess.
- **Gap coverage**: `t` between two segments → `none` + coverage pointing at the
  nearer neighbor.
- **Notification parity** (regression for the original bug): notification target
  `t` and the event-grid target for the same detection resolve to the *same*
  `media_offset`.
- **HLS sliding window**: seek into a DVR window after chunks roll off still maps
  `t`→correct frame via PDT, not via `seekable.start(0)` arithmetic.
