const test = require("node:test");
const assert = require("node:assert/strict");
const {
  MIN_SCALE,
  MAX_SCALE,
  clampState,
  resetState,
  zoomAt,
  pinchFactor,
  ElementZoom,
} = require("../src/wanyard/static/stage-zoom.js");

test("exports the shared DOM controller used by viewer and wall", () => {
  assert.equal(typeof ElementZoom, "function");
});

test("clampState keeps transformed content covering the stage", () => {
  let seed = 0x51f15e;
  const random = () => {
    seed = (1664525 * seed + 1013904223) >>> 0;
    return seed / 0x100000000;
  };

  for (let i = 0; i < 2000; i++) {
    const width = 1 + random() * 2000;
    const height = 1 + random() * 1200;
    const state = clampState({
      s: -4 + random() * 20,
      tx: -10000 + random() * 20000,
      ty: -10000 + random() * 20000,
    }, width, height);
    assert.ok(state.s >= MIN_SCALE && state.s <= MAX_SCALE);
    assert.ok(state.tx <= 0 && state.tx >= width - width * state.s);
    assert.ok(state.ty <= 0 && state.ty >= height - height * state.s);
  }
});

test("one-times zoom always has zero translation", () => {
  assert.deepEqual(clampState({ s: 1, tx: -500, ty: -200 }, 800, 450), resetState());
  assert.deepEqual(zoomAt({ s: 2, tx: -300, ty: -100 }, 0.01, { x: 50, y: 50 }, 800, 450), resetState());
});

test("zoomAt preserves the content coordinate beneath an unclamped anchor", () => {
  const before = { s: 2, tx: -400, ty: -225 };
  const anchor = { x: 400, y: 225 };
  const after = zoomAt(before, 1.4, anchor, 800, 450);
  const beforeContent = {
    x: (anchor.x - before.tx) / before.s,
    y: (anchor.y - before.ty) / before.s,
  };
  const afterContent = {
    x: (anchor.x - after.tx) / after.s,
    y: (anchor.y - after.ty) / after.s,
  };
  assert.ok(Math.abs(beforeContent.x - afterContent.x) < 1e-6);
  assert.ok(Math.abs(beforeContent.y - afterContent.y) < 1e-6);
});

test("reset round-trips from an arbitrary zoom", () => {
  const initial = resetState();
  const zoomed = zoomAt(resetState(), 4, { x: 321, y: 123 }, 1000, 600);
  assert.notDeepEqual(zoomed, initial);
  assert.deepEqual(resetState(), initial);
});

test("pinch factor is the ratio between pointer distances", () => {
  assert.equal(pinchFactor(100, 250), 2.5);
  assert.equal(pinchFactor(200, 100), 0.5);
  assert.equal(pinchFactor(0, 100), 1);
});
