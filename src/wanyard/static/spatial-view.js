(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.WanyardSpatialView = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  const CAMERA_FACING_FALLBACK = Object.freeze({ yaw: Math.PI, pitch: 0 });

  function cameraAlignedOrbit(modelSummary) {
    const rotation = modelSummary?.extrinsic?.[0];
    const cameraForward = rotation?.[2];
    if (!Array.isArray(cameraForward) || cameraForward.length < 3) {
      return { ...CAMERA_FACING_FALLBACK };
    }

    // VGGT extrinsics are world-to-camera in OpenCV coordinates. The third
    // row of R is therefore the camera's forward axis in world coordinates.
    // parsePly maps that world into viewer coordinates with diag(-1, -1, 1).
    const x = -Number(cameraForward[0]);
    const y = -Number(cameraForward[1]);
    const z = Number(cameraForward[2]);
    const horizontal = Math.hypot(x, z);
    const magnitude = Math.hypot(horizontal, y);
    if (![x, y, z, magnitude].every(Number.isFinite) || magnitude < 1e-8) {
      return { ...CAMERA_FACING_FALLBACK };
    }

    // The WebGL camera looks down -Z. Rotate the anchor camera's forward
    // vector onto -Z so the reconstruction initially faces the same way.
    return {
      yaw: Math.atan2(x, -z),
      pitch: Math.atan2(-y, horizontal),
    };
  }

  return { CAMERA_FACING_FALLBACK, cameraAlignedOrbit };
});
