(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.WanyardStageZoom = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  const MIN_SCALE = 1;
  const MAX_SCALE = 8;

  function finite(value, fallback) {
    value = Number(value);
    return Number.isFinite(value) ? value : fallback;
  }

  function clamp(value, low, high) {
    return Math.max(low, Math.min(high, value));
  }

  function clampState(state, width, height) {
    const w = Math.max(0, finite(width, 0));
    const h = Math.max(0, finite(height, 0));
    const s = clamp(finite(state?.s, MIN_SCALE), MIN_SCALE, MAX_SCALE);
    if (s === MIN_SCALE) return { s: MIN_SCALE, tx: 0, ty: 0 };
    return {
      s,
      tx: clamp(finite(state?.tx, 0), w - w * s, 0),
      ty: clamp(finite(state?.ty, 0), h - h * s, 0),
    };
  }

  function resetState() {
    return { s: MIN_SCALE, tx: 0, ty: 0 };
  }

  function zoomAt(state, factor, point, width, height) {
    const current = clampState(state, width, height);
    const nextScale = clamp(current.s * finite(factor, 1), MIN_SCALE, MAX_SCALE);
    const ratio = nextScale / current.s;
    const x = finite(point?.x, Math.max(0, finite(width, 0)) / 2);
    const y = finite(point?.y, Math.max(0, finite(height, 0)) / 2);
    return clampState({
      s: nextScale,
      tx: x - (x - current.tx) * ratio,
      ty: y - (y - current.ty) * ratio,
    }, width, height);
  }

  function pinchFactor(previousDistance, nextDistance) {
    const before = finite(previousDistance, 0);
    const after = finite(nextDistance, 0);
    return before > 0 && after > 0 ? after / before : 1;
  }

  return {
    MIN_SCALE,
    MAX_SCALE,
    clampState,
    resetState,
    zoomAt,
    pinchFactor,
  };
});
