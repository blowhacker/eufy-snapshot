const test = require("node:test");
const assert = require("node:assert/strict");
const {
  CAMERA_FACING_FALLBACK,
  cameraAlignedOrbit,
} = require("../src/wanyard/static/spatial-view.js");

const EPSILON = 1e-10;

function assertNear(actual, expected) {
  assert.ok(Math.abs(actual - expected) < EPSILON, `${actual} is not near ${expected}`);
}

test("faces an identity anchor camera instead of showing its view backwards", () => {
  const view = cameraAlignedOrbit({
    extrinsic: [[
      [1, 0, 0, 0],
      [0, 1, 0, 0],
      [0, 0, 1, 0],
    ]],
  });

  assertNear(Math.abs(view.yaw), Math.PI);
  assertNear(view.pitch, 0);
});

test("derives yaw from the reconstructed anchor camera rotation", () => {
  const view = cameraAlignedOrbit({
    extrinsic: [[
      [0, 0, -1, 0],
      [0, 1, 0, 0],
      [1, 0, 0, 0],
    ]],
  });

  assertNear(view.yaw, -Math.PI / 2);
  assertNear(view.pitch, 0);
});

test("derives pitch from the reconstructed anchor camera rotation", () => {
  const rootHalf = Math.SQRT1_2;
  const view = cameraAlignedOrbit({
    extrinsic: [[
      [1, 0, 0, 0],
      [0, rootHalf, -rootHalf, 0],
      [0, rootHalf, rootHalf, 0],
    ]],
  });

  assertNear(Math.abs(view.yaw), Math.PI);
  assertNear(view.pitch, Math.PI / 4);
});

test("uses a camera-facing fallback for legacy and malformed summaries", () => {
  assert.deepEqual(cameraAlignedOrbit(null), CAMERA_FACING_FALLBACK);
  assert.deepEqual(cameraAlignedOrbit({ extrinsic: [[[1], [0], [NaN, 0, 1]]] }), CAMERA_FACING_FALLBACK);
});
