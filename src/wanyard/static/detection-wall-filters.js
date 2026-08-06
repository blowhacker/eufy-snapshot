(function initDetectionWallFilters(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.DetectionWallFilters = api;
})(typeof window !== "undefined" ? window : globalThis, function buildFilters() {
  function parse(raw) {
    const values = String(raw || "")
      .split(",")
      .map(value => value.trim())
      .filter(Boolean);
    if (!values.length || values.includes("all")) return null;
    return new Set(values);
  }

  function serialize(filter) {
    if (filter == null) return "";
    return [...filter].filter(Boolean).sort().join(",");
  }

  function toggle(filter, sourceId, availableSourceIds = []) {
    if (!sourceId || sourceId === "all") return null;
    if (filter == null) return new Set([sourceId]);

    const next = new Set(filter);
    if (next.has(sourceId)) {
      // An empty wall is not a useful camera state. "All" remains the way to
      // expand a single-camera selection back to every camera.
      if (next.size === 1) return next;
      next.delete(sourceId);
    } else {
      next.add(sourceId);
    }

    const available = [...new Set(availableSourceIds.filter(Boolean))];
    if (available.length && available.every(id => next.has(id))) return null;
    return next;
  }

  function active(filter, sourceId) {
    return sourceId === "all" ? filter == null : Boolean(filter?.has(sourceId));
  }

  return { parse, serialize, toggle, active };
});
