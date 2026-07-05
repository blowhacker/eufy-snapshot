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

  class ElementZoom {
    constructor(stage, layer, resetButton, canInteract = () => true, options = {}) {
      this.stage = stage;
      this.layer = layer;
      this.resetButton = resetButton;
      this.canInteract = canInteract;
      this.ignoreSelector = options.ignoreSelector
        ?? ".stage-overlay,.v2-zone-bar,button,a,input,select,textarea";
      this.value = resetState();
      this.pointers = new Map();
      this.pan = null;
      this.pinch = null;
      this.justPannedUntil = 0;

      stage.addEventListener("wheel", e => this.onWheel(e), { passive: false });
      stage.addEventListener("pointerdown", e => this.onPointerDown(e));
      stage.addEventListener("pointermove", e => this.onPointerMove(e));
      stage.addEventListener("pointerup", e => this.onPointerEnd(e));
      stage.addEventListener("pointercancel", e => this.onPointerEnd(e));
      stage.addEventListener("dblclick", e => this.onDoubleClick(e));
      resetButton?.addEventListener("click", e => {
        e.stopPropagation();
        this.reset();
      });

      this.resizeObserver = typeof ResizeObserver === "function"
        ? new ResizeObserver(() => this.reclamp())
        : null;
      this.resizeObserver?.observe(stage);
      this.onResize = () => this.reclamp();
      window.addEventListener("resize", this.onResize);
      this.apply();
    }

    get state() {
      return { ...this.value };
    }

    size() {
      return { width: this.stage.clientWidth || 0, height: this.stage.clientHeight || 0 };
    }

    point(evt) {
      const rect = this.stage.getBoundingClientRect();
      return { x: evt.clientX - rect.left, y: evt.clientY - rect.top };
    }

    isChromeTarget(target) {
      return !!this.ignoreSelector
        && target instanceof Element
        && !!target.closest(this.ignoreSelector);
    }

    accepts(evt) {
      return this.canInteract(evt) && !this.isChromeTarget(evt.target);
    }

    set(next) {
      const { width, height } = this.size();
      this.value = clampState(next, width, height);
      this.apply();
    }

    apply() {
      const { s, tx, ty } = this.value;
      this.layer.style.transform = `translate(${tx}px, ${ty}px) scale(${s})`;
      const zoomed = s > 1.000001;
      this.layer.classList.toggle("zoomed", zoomed);
      if (this.resetButton) {
        this.resetButton.hidden = !zoomed;
        this.resetButton.textContent = `${s.toFixed(1).replace(/\.0$/, "")}× ⟲`;
      }
    }

    reclamp() {
      this.set(this.value);
    }

    reset() {
      this.value = resetState();
      this.pan = null;
      this.pinch = null;
      this.layer.classList.remove("panning");
      this.apply();
    }

    zoomBy(factor, point = null) {
      const { width, height } = this.size();
      const anchor = point || { x: width / 2, y: height / 2 };
      this.value = zoomAt(this.value, factor, anchor, width, height);
      this.apply();
    }

    onWheel(evt) {
      if (!this.accepts(evt)) return;
      evt.preventDefault();
      this.zoomBy(Math.exp(-evt.deltaY * 0.0015), this.point(evt));
    }

    onDoubleClick(evt) {
      if (!this.accepts(evt)) return;
      evt.preventDefault();
      if (this.value.s > 1.000001) this.reset();
      else this.zoomBy(2.5, this.point(evt));
    }

    capture(evt) {
      try { evt.target.setPointerCapture?.(evt.pointerId); } catch {}
    }

    onPointerDown(evt) {
      if (!this.accepts(evt) || (evt.pointerType === "mouse" && evt.button !== 0)) return;
      if (evt.pointerType === "mouse" && this.value.s <= 1) return;
      const point = this.point(evt);
      this.pointers.set(evt.pointerId, point);

      if (this.pointers.size >= 2) {
        this.capture(evt);
        evt.preventDefault();
        this.beginPinch();
        return;
      }

      if (this.value.s > 1) {
        this.capture(evt);
        evt.preventDefault();
        this.pan = {
          pointerId: evt.pointerId,
          start: point,
          tx: this.value.tx,
          ty: this.value.ty,
          moved: false,
        };
      }
    }

    beginPinch() {
      const points = [...this.pointers.values()].slice(0, 2);
      if (points.length < 2) return;
      const midpoint = {
        x: (points[0].x + points[1].x) / 2,
        y: (points[0].y + points[1].y) / 2,
      };
      this.pinch = {
        distance: Math.hypot(points[1].x - points[0].x, points[1].y - points[0].y),
        midpoint,
        state: this.state,
        moved: false,
      };
      this.pan = null;
    }

    onPointerMove(evt) {
      if (!this.pointers.has(evt.pointerId)) return;
      const point = this.point(evt);
      this.pointers.set(evt.pointerId, point);

      if (this.pointers.size >= 2 && this.pinch) {
        evt.preventDefault();
        const points = [...this.pointers.values()].slice(0, 2);
        const midpoint = {
          x: (points[0].x + points[1].x) / 2,
          y: (points[0].y + points[1].y) / 2,
        };
        const distance = Math.hypot(points[1].x - points[0].x, points[1].y - points[0].y);
        const factor = pinchFactor(this.pinch.distance, distance);
        const { width, height } = this.size();
        const zoomed = zoomAt(this.pinch.state, factor, this.pinch.midpoint, width, height);
        this.set({
          ...zoomed,
          tx: zoomed.tx + midpoint.x - this.pinch.midpoint.x,
          ty: zoomed.ty + midpoint.y - this.pinch.midpoint.y,
        });
        this.pinch.moved = this.pinch.moved
          || Math.abs(distance - this.pinch.distance) > 4
          || Math.hypot(midpoint.x - this.pinch.midpoint.x, midpoint.y - this.pinch.midpoint.y) > 4;
        if (this.pinch.moved) this.layer.classList.add("panning");
        return;
      }

      if (!this.pan || this.pan.pointerId !== evt.pointerId || this.value.s <= 1) return;
      const dx = point.x - this.pan.start.x;
      const dy = point.y - this.pan.start.y;
      if (!this.pan.moved && Math.hypot(dx, dy) <= 4) return;
      this.pan.moved = true;
      evt.preventDefault();
      this.layer.classList.add("panning");
      this.set({ s: this.value.s, tx: this.pan.tx + dx, ty: this.pan.ty + dy });
    }

    onPointerEnd(evt) {
      if (!this.pointers.has(evt.pointerId)) return;
      const moved = !!this.pan?.moved || !!this.pinch?.moved;
      this.pointers.delete(evt.pointerId);
      try { evt.target.releasePointerCapture?.(evt.pointerId); } catch {}

      if (this.pointers.size >= 2) {
        this.beginPinch();
      } else if (this.pointers.size === 1 && this.value.s > 1) {
        const [pointerId, point] = this.pointers.entries().next().value;
        this.pinch = null;
        this.pan = { pointerId, start: point, tx: this.value.tx, ty: this.value.ty, moved: false };
      } else {
        this.pan = null;
        this.pinch = null;
      }

      if (moved) this.justPannedUntil = performance.now() + 500;
      if (!this.pan?.moved && !this.pinch?.moved) this.layer.classList.remove("panning");
    }

    consumeJustPanned() {
      if (performance.now() > this.justPannedUntil) return false;
      this.justPannedUntil = 0;
      return true;
    }

    destroy() {
      this.resizeObserver?.disconnect();
      window.removeEventListener("resize", this.onResize);
    }
  }

  return {
    MIN_SCALE,
    MAX_SCALE,
    clampState,
    resetState,
    zoomAt,
    pinchFactor,
    ElementZoom,
  };
});
