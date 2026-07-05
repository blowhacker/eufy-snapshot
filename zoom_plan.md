# Video zoom in the viewer — implementation plan

Status: not started. Target: the single-camera viewer page (video2). Digital
zoom (pan + magnify) over live and recorded playback, boxes and zones staying
glued to the frame. Pure frontend; no backend changes.

## Grounding facts (verified against the code, 2026-07-05)

- Stage DOM (`src/wanyard/static/video2.html` ~line 60):

      <section class="image-stage v2-stage">
        <video id="v2Video" playsinline></video>
        <video id="v2LiveVideo" class="v2-live-video" autoplay muted playsinline></video>
        <canvas id="v2BoxCanvas"></canvas>
        <canvas id="v2ZoneCanvas"></canvas>
        <div id="v2Empty" class="empty">…</div>
        <div class="stage-overlay">…timestamp card + stage tools…</div>
      </section>

- `.v2-stage` has `position:relative; overflow:hidden` (video2.css ~419).
  Videos are `object-fit:contain; width/height:100%`; both canvases are
  `position:absolute; inset:0`.
- `drawBoxList(v, boxes)` (video2.js ~4627) sizes the canvas from
  `clientWidth/clientHeight` (LAYOUT size — CSS transforms do not change it)
  and computes letterboxing itself from `videoWidth/videoHeight`. Therefore a
  CSS transform applied to video+canvas together keeps boxes glued with ZERO
  drawing changes.
- Zone-editor pointer mapping normalizes with
  `getBoundingClientRect()` (rect.left/rect.width) — rects DO reflect CSS
  transforms, so normalized zone coords remain correct under zoom. During
  implementation, grep the zone/box hit-test code for any mapping that uses
  `clientWidth` instead of the bounding rect and switch it to rect.
- NAMING HAZARD: the timeline already has `_applyZoom` (video2.js ~3423) —
  that is TIME-AXIS zoom on `#v2TlCanvas`. Do not touch it; name everything
  here `stageZoom` / `StageZoom` for grep-unambiguity.
- The video element click toggles playback; only the timeline canvas has a
  dblclick handler today — the video itself has none (verified by grep).
- Asset cache-busting convention: bump the `?v=` suffix on
  `/video2.js`, `/video2.css` in `video2.html` (current: check file; suffixes
  are feature-named, e.g. `?v=per-camera-retention-2`).
- Existing keyboard shortcuts live in video2.js — search
  `addEventListener("keydown"` and check `+`, `-`, `0`, `Escape` for
  collisions before binding.
- JS test convention: `tests/test_live_sei_clock.js` is a plain-node test run
  with `node tests/test_live_sei_clock.js` style (UMD module export from the
  static file). Follow that pattern for the zoom math.

## Design

### One transformed layer

Add a wrapper inside the stage holding ONLY the media + annotation layers:

      <section class="image-stage v2-stage">
        <div id="v2StageZoom">
          <video id="v2Video" …>
          <video id="v2LiveVideo" …>
          <canvas id="v2BoxCanvas"></canvas>
          <canvas id="v2ZoneCanvas"></canvas>
        </div>
        <div id="v2Empty" …>            <!-- outside: never zooms -->
        <div class="stage-overlay" …>   <!-- outside: never zooms -->
      </section>

CSS:

      #v2StageZoom {
        position: absolute; inset: 0;
        transform-origin: 0 0;
        will-change: transform;
      }
      #v2StageZoom.panning { cursor: grabbing; }
      /* only when zoomed, so normal behavior survives at 1x */
      #v2StageZoom.zoomed { touch-action: none; cursor: grab; }

CHECK: the two videos are currently `position:relative` + display toggled
between them; moving them into an absolute wrapper must preserve their
show/hide logic (`el.video.style.display` / `el.liveVideo.style.display`) —
the wrapper changes no display logic, only adds an ancestor. Ensure z-index
stack (`.empty` z-index 3, canvases 4) still layers correctly with the
wrapper interposed; give the wrapper `z-index: 3` if needed.

Apply `transform: translate(txpx, typx) scale(s)` on the wrapper. UI overlay
and empty-state stay crisp and unzoomed.

### State + math (pure, unit-testable)

State: `{ s, tx, ty }`, `s ∈ [1, 8]`, translate in px, origin top-left.

Clamp — content must always cover the viewport (no gaps at any edge):

      // w, h = stage layout size (stage.clientWidth/Height)
      tx ∈ [w - w*s, 0]
      ty ∈ [h - h*s, 0]
      s  ∈ [1, 8];  if s === 1 then tx = ty = 0

Zoom anchored at a stage-local point p (keep the content pixel under the
cursor stationary):

      s2 = clamp(s * factor)
      tx2 = p.x - (p.x - tx) * (s2 / s)
      ty2 = p.y - (p.y - ty) * (s2 / s)
      then clamp tx2/ty2

On stage RESIZE (window resize / fullscreen): re-clamp with new w/h (simplest
correct behavior; do not try to preserve the center point in v1).

Ship the math as pure functions on the module (same UMD style as
live-sei-clock.js if extracted, or plainly exported for tests via
`window.WanyardStageZoom` + `module.exports` guard) so node tests can hit it
without a DOM.

### Interactions

Desktop:
- `wheel` on the stage: `factor = Math.exp(-e.deltaY * 0.0015)`, anchor =
  cursor position relative to the stage rect. `preventDefault()`. Plain wheel
  (the stage does not scroll); if it proves annoying the fallback decision is
  ctrl+wheel — leave a one-line switch.
- Drag-to-pan when `s > 1`: pointerdown → track; only treat as pan after
  movement exceeds 4px (so the existing click-to-toggle-playback still fires
  for clicks); `setPointerCapture`; suppress the synthetic click after a pan
  (click handler checks a `justPanned` flag set on pointerup after real
  movement).
- `dblclick` on the stage: toggle 1x ↔ 2.5x anchored at the pointer.
- Keys: `+`/`=` step in ×1.4 at center, `-` step out, `0` or `Escape` reset.
  CHECK collisions with existing keydown handlers first; if Escape is taken
  (e.g. zone editor cancel), keep Escape for its existing use when that mode
  is active — zoom reset only otherwise.

Touch (Pointer Events on the same controller — no separate touch code):
- Maintain a `Map(pointerId → point)`. Two pointers: pinch — factor from the
  distance ratio, anchor at the midpoint; also pan by midpoint movement.
- One pointer while `s > 1`: pan.
- `touch-action: none` comes from the `.zoomed` class only (see CSS), so
  page scrolling is untouched at 1×.

Affordance:
- A chip in `.stage-tools` (next to fullscreen): `2.4× ⟲`, visible only when
  `s > 1`, click = reset. Follow the styling of existing stage tool buttons.

Policy:
- Zoom persists across play/pause/seek/live-DVR flips within a camera.
- Reset on camera switch (hook the same place `renderSrcCtrl`'s click handler
  resets per-source state, video2.js ~2747: `st.source = s.id; …`).
- Reset on fullscreen enter/exit (find the fullscreen button handler in
  stage tools; it fullscreens the stage — transform works fullscreen, but
  reset avoids disorientation).

### Correctness notes for the implementer

- DO NOT touch `drawBoxList`, the SEI clock, rVFC code, or the zone editor's
  normalized math — the whole point of transforming the wrapper is that they
  keep working. If boxes appear to drift under zoom, the bug is a mapping
  that used `clientWidth` (layout) where it needed `getBoundingClientRect()`
  (visual) — fix the mapping, not the drawing.
- Box stroke widths scale visually with `s` (thicker when zoomed). Accepted
  for v1. (Optional later: divide lineWidth by current scale in drawBoxList —
  requires exposing `stageZoom.s`; do not do it in v1.)
- The `.empty` overlay and live-only message are outside the wrapper —
  verify they still center (they use flex; unaffected).
- Wheel listener must be `{ passive: false }` to allow preventDefault.
- Do not bind wheel/pointer on `#v2TlCanvas` (timeline) — stage only.

## Out of scope (v1)

- Wall tiles (click-through to the viewer is the wall's zoom; the wall
  already has a tile-size slider).
- "Zoom to event" (auto-frame a detection box) — shape the controller with a
  `zoomToRect({x1,y1,x2,y2})` method signature in mind (normalized frame
  coords → letterbox-corrected stage rect → s/tx/ty), but do not build it.
- Persisting zoom per camera across reloads.

## Files touched

- `src/wanyard/static/video2.html` — wrapper div + chip button + `?v=` bumps.
- `src/wanyard/static/video2.css` — wrapper, `.zoomed`, chip styles.
- `src/wanyard/static/video2.js` — `StageZoom` controller (~180 lines), init
  after `el` map, camera-switch + fullscreen reset hooks.
- `tests/test_stage_zoom.js` — node tests for the pure math.

## Tests

Node (pure math): clamp never exposes a gap for random (s, tx, ty, w, h);
`zoomAt` keeps the anchor point's content position invariant
(|before − after| < 1e-6); s=1 forces tx=ty=0; reset round-trips; pinch
factor from two-pointer distances.

Manual matrix (record what you see, per item):
1. Live playback, zoom 4×, people walking — boxes glued to bodies while
   panning.
2. Recorded scrub at 4× — boxes glued across seeks; SEI clock source still
   `sei` (check `player.frameClockStatus` / clock debug if exposed).
3. Zone editor: draw + drag a zone while zoomed — saved polygon lands where
   drawn (reload at 1× to verify placement).
4. Narrow window (portrait letterbox): zoom anchors correctly inside the
   letterboxed video area (anchor math is stage-relative, letterbox is inside
   the video element — acceptable that anchoring is stage-relative, note it).
5. iPhone Safari: pinch/pan; page scroll still works at 1×.
6. Fullscreen enter/exit resets; wheel zoom works in fullscreen.
7. Camera switch resets; play/pause/seek/live-toggle do NOT reset.
8. Click-to-pause still works at 1× and when zoomed (no pan movement).

## Deploy

Standard: branch → tests → `scripts/deploy.sh` (deploys current branch to
banana; it verifies GPU wiring and fails loudly). Frontend-only change, but
static assets are baked into the image → the deploy's `--build` is required.
Bump `?v=` suffixes or the browser serves stale JS.
