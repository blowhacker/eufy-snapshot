// ═══════════════════════════════════════════════════════
// V2Player — transparent, segment-aware, async seek
// ═══════════════════════════════════════════════════════
class V2Player {
  #v;
  #segs = [];
  #abort = null;
  #activeSeg = null;
  #hls = null;
  #hlsUrl = null;
  #lastSeek = null;
  #rate = 1;
  #intendedTs = null;  // display target while async seek/load is in flight
  #presentedMediaTime = null;
  #listeners = { timeupdate: new Set(), frame: new Set(), play: new Set(), pause: new Set(), ended: new Set() };

  constructor(videoEl) {
    this.#v = videoEl;
    this.#v.addEventListener("timeupdate", () => this.#emit("timeupdate"));
    this.#v.addEventListener("play",       () => this.#emit("play"));
    this.#v.addEventListener("pause",      () => this.#emit("pause"));
    this.#v.addEventListener("ended",      () => this.#emit("ended"));
    this.#startFrameClock();
  }

  setSegments(segs) {
    this.#segs = [...segs].sort((a, b) => a.start_ts - b.start_ts);
    if (this.#activeSeg && !this.#activeSeg.replay_hls) {
      this.#activeSeg = this.#segs.find(s => s.id === this.#activeSeg.id) ?? this.#activeSeg;
    }
  }

  // ── Seek ──────────────────────────────────────────────
  // direction: "backward" | "forward" | null — hints gap resolution
  async seek(unix_ts, srcId = null, direction = null) {
    this.#abort?.abort();
    this.#abort = new AbortController();
    const { signal } = this.#abort;

    const segDirect = this.#segFor(unix_ts, srcId);
    const seg = segDirect ?? this.#resolve(unix_ts, srcId, direction);
    if (!seg) {
      if (this.#abort?.signal === signal) this.#intendedTs = null;
      return null;
    }

    // Clamp offset to within segment duration — prevents snapping past end
    const maxOff = seg.end_ts ? Math.max(0, seg.end_ts - seg.start_ts - 0.5) : Infinity;
    const offset = Math.max(0, Math.min(unix_ts - seg.start_ts, maxOff));
    const url    = `/video/files/${seg.path}`;
    const actualTs = seg.start_ts + offset;
    const landing = {
      requestedTs: unix_ts,
      actualTs,
      offsetSecs: offset,
      remainingSecs: seg.end_ts == null ? Infinity : Math.max(0, seg.end_ts - actualTs),
      reason: segDirect ? "direct" : (direction ? `gap-${direction}` : "gap-nearest"),
      sourceId: seg.source_id,
      segmentId: seg.id,
      segment: seg,
    };

    this.#intendedTs = actualTs;
    this.#presentedMediaTime = offset;

    try {
      this.#destroyHls();
      if (this.#v.dataset.src !== url) {
        this.#v.src         = url;
        this.#v.dataset.src = url;
        this.#applyRate();
        this.#v.load();
        await this.#waitFor("loadedmetadata", signal);
        this.#applyRate();
      }

      if (signal.aborted) return null;

      this.#activeSeg = seg;
      if (Math.abs((this.#v.currentTime || 0) - offset) > 0.05) {
        const seeked = this.#waitFor("seeked", signal);
        this.#v.currentTime = offset;
        await seeked;
      } else {
        this.#v.currentTime = offset;
      }
    } catch {
      if (this.#abort?.signal === signal && this.#intendedTs === actualTs) this.#intendedTs = null;
      return null;
    }

    if (signal.aborted) return null;
    this.#presentedMediaTime = offset;
    if (this.#intendedTs === actualTs) this.#intendedTs = null;
    this.#lastSeek = landing;
    this.#emit("timeupdate");
    return landing;
  }

  // Recorded seek seeded by the server resolver: play the MP4 file directly.
  // currentTs = media_epoch + presentedMediaTime, the same media_epoch detections
  // are anchored to, so overlay boxes enclose the on-screen frame. No HLS rewrap,
  // no PDT, no second clock — nothing to drift.
  async seekRecorded(opts = {}) {
    this.#abort?.abort();
    this.#abort = new AbortController();
    const { signal } = this.#abort;

    const url = String(opts.url || "");
    const mediaEpoch = Number(opts.mediaEpoch);
    const duration = Number.isFinite(Number(opts.duration)) ? Math.max(0, Number(opts.duration)) : Infinity;
    if (!url || !Number.isFinite(mediaEpoch)) return null;
    const maxOff = Number.isFinite(duration) ? Math.max(0, duration - 0.25) : Infinity;
    const startPosition = Math.max(0, Math.min(maxOff, Number(opts.startPosition) || 0));

    const actualTs = mediaEpoch + startPosition;
    const seg = {
      id: opts.segmentId ?? `seg:${url}`,
      source_id: opts.sourceId ?? null,
      start_ts: mediaEpoch,
      end_ts: Number.isFinite(duration) ? mediaEpoch + duration : null,
      path: url.replace(/^\/video\/files\//, ""),
      replay_hls: false,
    };
    const landing = {
      requestedTs: Number.isFinite(Number(opts.requestedTs)) ? Number(opts.requestedTs) : actualTs,
      actualTs,
      offsetSecs: startPosition,
      remainingSecs: Number.isFinite(duration) ? Math.max(0, duration - startPosition) : Infinity,
      reason: "recorded",
      sourceId: seg.source_id,
      segmentId: seg.id,
      segment: seg,
    };
    this.#intendedTs = actualTs;
    this.#presentedMediaTime = startPosition;

    try {
      this.#destroyHls();
      if (this.#v.dataset.src !== url) {
        this.#v.src         = url;
        this.#v.dataset.src = url;
        this.#applyRate();
        this.#v.load();
        await this.#waitFor("loadedmetadata", signal);
        this.#applyRate();
      }

      if (signal.aborted) return null;
      this.#activeSeg = seg;
      if (Math.abs((this.#v.currentTime || 0) - startPosition) > 0.05) {
        const seeked = this.#waitFor("seeked", signal);
        this.#v.currentTime = startPosition;
        await seeked;
      } else {
        this.#v.currentTime = startPosition;
      }
    } catch {
      if (this.#abort?.signal === signal && this.#intendedTs === actualTs) this.#intendedTs = null;
      return null;
    }

    if (signal.aborted) return null;
    this.#presentedMediaTime = startPosition;
    if (this.#intendedTs === actualTs) this.#intendedTs = null;
    this.#lastSeek = landing;
    this.#emit("timeupdate");
    return landing;
  }

  // ── Playback ──────────────────────────────────────────
  play()         { this.#applyRate(); return this.#v.play().catch(() => {}); }
  pause()        { this.#v.pause(); }
  setRate(rate)  { this.#rate = rate; this.#applyRate(); }
  get ended()    { return this.#v.ended; }
  // nextSegment: for app to call when 'ended' fires
  nextSegment(srcId) {
    const cur = this.currentSeg;
    if (!cur) return null;
    const src = srcId ?? cur.source_id;
    return this.#segs
      .filter(s => s.end_ts != null && s.source_id === src && s.start_ts >= (cur.end_ts ?? cur.start_ts))
      .sort((a, b) => a.start_ts - b.start_ts)[0] ?? null;
  }
  get paused()      { return this.#v.paused; }
  get intendedTs()  { return this.#intendedTs; }
  get displayTs()   { return this.#intendedTs ?? this.currentTs; }
  /** Backwards-compatible alias for the UI's display clock. */
  get reliableTs()  { return this.displayTs; }
  get mediaTs()     { return this.currentTs; }
  get lastSeek()    { return this.#lastSeek; }
  get duration() { return this.#v.duration || 0; }
  get remainingSecs() {
    if (this.#v.ended) return 0;
    const seg = this.currentSeg, ts = this.currentTs;
    if (!seg || seg.end_ts == null || ts == null) return null;
    return Math.max(0, seg.end_ts - ts);
  }
  get nearSegmentEnd() {
    const rem = this.remainingSecs;
    return this.#v.ended || (rem != null && rem <= 1.25);
  }

  // ── Current timestamp ─────────────────────────────────
  get currentTs() {
    const mediaTime = this.#currentMediaTime();
    if (!this.#activeSeg || mediaTime == null) return null;
    return this.#programDateTimeForMediaTime(mediaTime) ?? (this.#activeSeg.start_ts + mediaTime);
  }

  get currentSeg() {
    return this.#activeSeg;
  }

  // ── Clip playlist — returns PlaylistHandle ────────────
  playClips(clips, startIdx = 0) {
    const handle = new PlaylistHandle(this, clips, startIdx);
    handle._start();
    return handle;
  }

  // ── Events ────────────────────────────────────────────
  on(event, fn)  { this.#listeners[event]?.add(fn); }
  off(event, fn) { this.#listeners[event]?.delete(fn); }
  #emit(event)   { this.#listeners[event]?.forEach(fn => fn()); }

  #startFrameClock() {
    if (typeof this.#v.requestVideoFrameCallback !== "function") return;
    const tick = (_now, metadata) => {
      const mediaTime = Number(metadata?.mediaTime);
      if (Number.isFinite(mediaTime)) {
        this.#presentedMediaTime = mediaTime;
        this.#emit("frame");
      }
      this.#v.requestVideoFrameCallback(tick);
    };
    this.#v.requestVideoFrameCallback(tick);
  }

  #currentMediaTime() {
    const current = Number(this.#v.currentTime);
    const presented = Number(this.#presentedMediaTime);
    if (Number.isFinite(presented)) return Math.max(0, presented);
    return Number.isFinite(current) ? Math.max(0, current) : null;
  }

  #programDateTimeForMediaTime(mediaTime) {
    const level = this.#hls?.levels?.[this.#hls.currentLevel];
    const details = this.#hls?.latestLevelDetails || level?.details || this.#hls?.levels?.find(l => l.details)?.details;
    const fragments = details?.fragments || [];
    for (const frag of fragments) {
      const start = Number(frag.start);
      const duration = Number(frag.duration);
      const pdt = this.#programDateSeconds(frag.programDateTime);
      if (!Number.isFinite(start) || !Number.isFinite(duration) || pdt == null) continue;
      if (mediaTime >= start - 0.05 && mediaTime <= start + duration + 0.05) {
        return pdt + (mediaTime - start);
      }
    }
    return null;
  }

  #programDateSeconds(value) {
    if (value == null) return null;
    if (value instanceof Date) return value.getTime() / 1000;
    const n = Number(value);
    if (Number.isFinite(n)) return n > 1e11 ? n / 1000 : n;
    const parsed = Date.parse(String(value));
    return Number.isFinite(parsed) ? parsed / 1000 : null;
  }

  #applyRate() {
    this.#v.defaultPlaybackRate = this.#rate;
    this.#v.playbackRate = this.#rate;
  }

  #destroyHls() {
    if (this.#hls) {
      this.#hls.destroy();
      this.#hls = null;
    }
    this.#hlsUrl = null;
  }

  // ── Private helpers ───────────────────────────────────
  #segFor(ts, srcId) {
    // Only closed segments (end_ts set) are playable; open files lack moov atom
    const pool = (srcId ? this.#segs.filter(s => s.source_id === srcId) : this.#segs)
      .filter(s => s.end_ts != null);
    return pool.find(s => s.start_ts <= ts && s.end_ts > ts) ?? null;
  }

  #resolve(ts, srcId, direction) {
    const pool = (srcId ? this.#segs.filter(s => s.source_id === srcId) : this.#segs)
      .filter(s => s.end_ts != null);
    if (!pool.length) return null;
    if (direction === "backward")
      // Latest segment ending at or before ts
      return pool.filter(s => s.end_ts <= ts).sort((a, b) => b.end_ts - a.end_ts)[0] ?? null;
    if (direction === "forward")
      // Earliest segment starting at or after ts
      return pool.filter(s => s.start_ts >= ts).sort((a, b) => a.start_ts - b.start_ts)[0] ?? null;
    // No direction hint: nearest edge
    const dist = s => Math.max(0, s.start_ts - ts, ts - s.end_ts);
    return pool.reduce((best, s) => !best || dist(s) < dist(best) ? s : best, null);
  }

  #waitFor(event, signal, timeoutMs = 10000) {
    return new Promise((resolve, reject) => {
      if (signal.aborted) { reject(new DOMException("aborted")); return; }
      let timer = null;
      const cleanup = () => {
        if (timer) clearTimeout(timer);
        signal.removeEventListener("abort", onAbort);
        this.#v.removeEventListener(event, onEvent);
      };
      const onEvent = () => { cleanup(); resolve(); };
      const onAbort = () => { cleanup(); reject(new DOMException("aborted")); };
      timer = setTimeout(() => { cleanup(); reject(new Error(`${event} timeout`)); }, timeoutMs);
      this.#v.addEventListener(event, onEvent, { once: true });
      signal.addEventListener("abort", onAbort, { once: true });
    });
  }

  #waitForHls(hls, HlsCtor, event, signal, timeoutMs = 10000) {
    return new Promise((resolve, reject) => {
      if (signal.aborted) { reject(new DOMException("aborted")); return; }
      let timer = null;
      const cleanup = () => {
        if (timer) clearTimeout(timer);
        signal.removeEventListener("abort", onAbort);
        hls.off(event, onEvent);
        hls.off(HlsCtor.Events.ERROR, onError);
      };
      const onEvent = () => { cleanup(); resolve(); };
      const onAbort = () => { cleanup(); reject(new DOMException("aborted")); };
      const onError = (_, data) => {
        if (!data?.fatal) return;
        cleanup();
        reject(new Error(data.details || "HLS error"));
      };
      timer = setTimeout(() => { cleanup(); reject(new Error(`${event} timeout`)); }, timeoutMs);
      hls.on(event, onEvent);
      hls.on(HlsCtor.Events.ERROR, onError);
      signal.addEventListener("abort", onAbort, { once: true });
    });
  }
}

// ═══════════════════════════════════════════════════════
// PlaylistHandle — caller-owned clip sequence
// ═══════════════════════════════════════════════════════
class PlaylistHandle {
  #player;
  #clips;   // [[start_ts, end_ts], ...]
  #idx = 0;
  #active = false;
  #check;
  onEnd = null;

  constructor(player, clips, startIdx = 0) {
    this.#player = player;
    this.#clips  = clips;
    this.#idx    = Math.max(0, Math.min(clips.length - 1, startIdx));
    this.#check  = () => this.#watchEnd();
  }

  _start() {
    if (this.#active) return;
    if (this.#idx >= this.#clips.length) this.#idx = 0;
    this.#active = true;
    this.#player.on("timeupdate", this.#check);
    this.#player.on("ended",      this.#check); // catch segment file end
    this.#seekCurrent();
  }

  async #seekCurrent() {
    if (!this.#active || this.#idx >= this.#clips.length) return;
    const [start, , srcId] = this.#clips[this.#idx];
    const landing = await this.#player.seek(start, srcId);
    if (!this.#active) return;  // cancelled during seek
    if (!landing) return;
    this.#player.play();
  }

  #watchEnd() {
    if (!this.#active) return;
    const [start, end, srcId] = this.#clips[this.#idx] ?? [];
    const ts = this.#player.currentTs;
    if (end != null && ts != null && ts >= end) { this.#advance(); return; }

    if (this.#player.ended && end != null) {
      const next = this.#player.nextSegment(srcId);
      if (next && next.start_ts < end) {
        this.#player.seek(Math.max(next.start_ts, start), next.source_id, "forward")
          .then(landing => { if (this.#active && landing) this.#player.play(); });
      } else {
        this.#advance();
      }
    }
  }

  #advance() {
    this.#idx++;
    if (this.#idx < this.#clips.length) {
      this.#seekCurrent();
    } else {
      this.cancel();
      this.onEnd?.();
    }
  }

  next() { if (this.#active) { this.#idx = Math.min(this.#clips.length - 1, this.#idx + 1); this.#seekCurrent(); } }
  prev() { if (this.#active) { this.#idx = Math.max(0, this.#idx - 1); this.#seekCurrent(); } }
  get clipIdx()   { return this.#idx; }
  get clipCount() { return this.#clips.length; }

  restart() {
    this.cancel();
    this.#idx = 0;
    this._start();
  }

  cancel() {
    this.#active = false;
    this.#player.off("timeupdate", this.#check);
    this.#player.off("ended",      this.#check);
  }
}

// ═══════════════════════════════════════════════════════
// AppMode — explicit state machine, owns PlaylistHandle
// ═══════════════════════════════════════════════════════
class AppMode {
  #player;
  #handle = null;
  #mode = "seek";   // "seek" | "playlist" | "live"
  #op = 0;
  onModeChange = null;

  constructor(player) { this.#player = player; }

  get current() { return this.#mode; }

  seekTo(unix_ts, srcId = null, direction = null, options = {}) {
    this.#cancel();
    this.#mode = "seek";
    this.onModeChange?.("seek");
    const op = ++this.#op;
    const autoplay = options.autoplay !== false;
    this.#player.seek(unix_ts, srcId, direction).then(landing => {
      if (op !== this.#op || !landing) return;
      const shortBackwardGap = landing.reason === "gap-backward" && landing.remainingSecs <= 1.25;
      if (autoplay && !shortBackwardGap) this.#player.play();
    });
  }

  playEventPlaylist(events, loop = true, startIdx = 0) {
    if (!events.length) return;
    this.#cancel();
    this.#mode = "playlist";
    this.onModeChange?.("playlist");
    const POST = 10;
    const clips = events.map(e => [e.abs_ts, e.abs_ts + (e.end_off - e.start_off) + POST, e.source_id]);
    this.#handle = this.#player.playClips(clips, startIdx);
    this.#handle.onEnd = () => {
      if (loop && this.#mode === "playlist") this.#handle.restart();
      else { this.#mode = "seek"; this.onModeChange?.("seek"); }
    };
    return this.#handle;
  }

  playClipRange(start, end, sourceId, { loop = true } = {}) {
    if (start == null || end == null || !sourceId || end <= start) return null;
    this.#cancel();
    this.#mode = "playlist";
    this.onModeChange?.("playlist");
    this.#handle = this.#player.playClips([[start, end, sourceId]], 0);
    this.#handle.onEnd = () => {
      if (loop && this.#mode === "playlist") this.#handle.restart();
      else { this.#mode = "seek"; this.onModeChange?.("seek"); }
    };
    return this.#handle;
  }

  goLive(srcId, segments) {
    this.#cancel();
    this.#mode = "live";
    this.onModeChange?.("live");
    const op = ++this.#op;
    const latest = [...segments].sort((a, b) => b.start_ts - a.start_ts)
      .find(s => (s.source_id === srcId || !srcId) && s.end_ts != null);
    if (latest) this.#player.seek(Math.max(latest.start_ts, latest.end_ts - 1), latest.source_id, "backward")
      .then(landing => { if (op === this.#op && landing) this.#player.play(); });
  }

  enterLive() {
    this.#cancel();
    this.#mode = "live";
    this.onModeChange?.("live");
  }

  stopLive() {
    if (this.#mode !== "live") return;
    this.#op++;
    this.#mode = "seek";
    this.onModeChange?.("seek");
  }

  stop() {
    this.#cancel();
    this.#mode = "seek";
    this.onModeChange?.("seek");
  }

  playFromCurrent(srcId = null) {
    if (this.#mode === "playlist") {
      this.#player.play();
      return;
    }
    const seg = this.#player.currentSeg;
    if (seg && this.#player.nearSegmentEnd) {
      const next = this.#player.nextSegment(srcId ?? seg.source_id);
      if (next) {
        this.seekTo(next.start_ts, next.source_id, "forward");
        return;
      }
      if (this.#player.ended) {
        const dur = (seg.end_ts ?? (seg.start_ts + this.#player.duration)) - seg.start_ts;
        this.seekTo(seg.start_ts + Math.max(0, dur - 30), seg.source_id);
        return;
      }
    }
    this.#player.play();
  }

  handleEnded(srcId = null) {
    if (this.#mode === "playlist") return;
    const seg = this.#player.currentSeg;
    const next = this.#player.nextSegment(srcId ?? seg?.source_id ?? null);
    if (next) this.seekTo(next.start_ts, next.source_id, "forward");
  }

  #cancel() {
    this.#op++;
    this.#handle?.cancel();
    this.#handle = null;
  }

  get handle() { return this.#handle; }
}

// ═══════════════════════════════════════════════════════
// V2Timeline — pure renderer + decode(x,y) helper
// ═══════════════════════════════════════════════════════
const EVENT_COLORS = {
  person:"#4ec98a",bird:"#78b7ff",cat:"#78b7ff",dog:"#78b7ff",
  car:"#e8a558",truck:"#f1788a",bus:"#cc9bff",motorcycle:"#7bd7c4",bicycle:"#d6ca72",
};
const EVENT_PALETTE = ["#78b7ff", "#4ec98a", "#e8a558", "#cc9bff", "#f1788a", "#7bd7c4", "#d6ca72"];

function classColor(cls) {
  if (EVENT_COLORS[cls]) return EVENT_COLORS[cls];
  let hash = 0;
  String(cls || "").split("").forEach(ch => { hash = ((hash << 5) - hash + ch.charCodeAt(0)) | 0; });
  return EVENT_PALETTE[Math.abs(hash) % EVENT_PALETTE.length];
}

function planTimeAxis(from, to, left, right, ctx, opts = {}) {
  const d3 = window.d3;
  const width = Math.max(1, right - left);
  const span = Math.max(1, to - from);
  const minLabelPx = opts.minLabelPx ?? 84;
  const minGridPx = opts.minGridPx ?? 52;
  const maxLabels = Math.max(2, Math.floor(width / minLabelPx));
  const maxGrid = Math.max(maxLabels, Math.floor(width / minGridPx));
  if (!d3?.scaleTime) return fallbackTimeAxis(from, to, left, right, ctx, { minLabelPx });

  const scale = d3.scaleTime()
    .domain([new Date(from * 1000), new Date(to * 1000)])
    .range([left, right]);
  const fmt = tickFormatter(span);
  const makeTick = d => ({
    ts: d.getTime() / 1000,
    x: scale(d),
    label: fmt(d),
    major: isMajorTimeTick(d, span),
  });
  const labels = filterTickCollisions(scale.ticks(maxLabels).map(makeTick), ctx, minLabelPx);
  const grid = scale.ticks(maxGrid).map(makeTick);
  return { grid, labels };
}

function fallbackTimeAxis(from, to, left, right, ctx, opts = {}) {
  const span = Math.max(1, to - from);
  const interval = fallbackTickInterval(span, Math.max(2, Math.floor((right - left) / (opts.minLabelPx ?? 84))));
  const fmt = tickFormatter(span);
  const ticks = [];
  for (let t = Math.ceil(from / interval) * interval; t <= to; t += interval) {
    const d = new Date(t * 1000);
    ticks.push({
      ts: t,
      x: left + ((t - from) / span) * Math.max(1, right - left),
      label: fmt(d),
      major: isMajorTimeTick(d, span),
    });
  }
  return { grid: ticks, labels: filterTickCollisions(ticks, ctx, opts.minLabelPx ?? 84) };
}

function filterTickCollisions(ticks, ctx, minLabelPx) {
  const out = [];
  let lastRight = -Infinity;
  for (const tick of ticks) {
    const measured = ctx?.measureText ? ctx.measureText(tick.label).width : minLabelPx;
    const half = Math.max(measured + 12, minLabelPx) / 2;
    if (tick.x - half <= lastRight + 8) continue;
    out.push(tick);
    lastRight = tick.x + half;
  }
  return out;
}

function tickFormatter(span) {
  const d3 = window.d3;
  const f = d3?.timeFormat;
  if (!f) {
    return d => span > 36 * 3600
      ? d.toLocaleDateString(undefined, { day: "2-digit", month: "short" })
      : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  const second = f("%H:%M:%S");
  const minute = f("%H:%M");
  const dayTime = f("%a %H:%M");
  const date = f("%d %b");
  const month = f("%b %Y");
  if (span > 90 * 86400) return month;
  if (span > 7 * 86400) return date;
  if (span > 36 * 3600) {
    return d => (d.getHours() === 0 && d.getMinutes() === 0) ? date(d) : minute(d);
  }
  if (span > 18 * 3600) return dayTime;
  if (span > 10 * 60) return minute;
  return second;
}

function isMajorTimeTick(d, span) {
  if (span > 90 * 86400) return d.getDate() === 1;
  if (span > 36 * 3600) return d.getHours() === 0 && d.getMinutes() === 0;
  if (span > 3 * 3600) return d.getMinutes() === 0;
  return d.getMinutes() % 15 === 0 && d.getSeconds() === 0;
}

function fallbackTickInterval(span, targetCount) {
  const target = span / Math.max(1, targetCount);
  const steps = [
    1, 5, 15, 30,
    60, 5 * 60, 15 * 60, 30 * 60,
    3600, 2 * 3600, 3 * 3600, 6 * 3600, 12 * 3600,
    86400, 2 * 86400, 7 * 86400, 30 * 86400, 90 * 86400,
  ];
  return steps.find(s => s >= target) ?? steps[steps.length - 1];
}

class V2Timeline {
  #c; #ctx;
  #segs = []; #evts = []; #srcNames = {};
  #from = 0; #to = 0;
  #head = null;
  #clip = null;
  #SRC_W = 78;
  #eventsRanges = [];
  #fetchFrom = 0; #fetchTo = 0;
  #fetchRaf = null;

  constructor(canvasEl) {
    this.#c   = canvasEl;
    this.#ctx = canvasEl.getContext("2d");
  }

  setWindow(from, to) { this.#from = from; this.#to = to; this.draw(); }
  setEventsRanges(ranges) { this.#eventsRanges = ranges; this.draw(); }
  setFetchingRange(from, to) {
    this.#fetchFrom = from; this.#fetchTo = to;
    if (!this.#fetchRaf) this.#animateFetch();
  }
  clearFetchingRange() {
    this.#fetchFrom = 0; this.#fetchTo = 0;
    if (this.#fetchRaf) { cancelAnimationFrame(this.#fetchRaf); this.#fetchRaf = null; }
    this.draw();
  }
  #animateFetch() {
    this.draw();
    this.#fetchRaf = requestAnimationFrame(() => {
      if (this.#fetchTo > this.#fetchFrom) this.#animateFetch();
      else this.#fetchRaf = null;
    });
  }
  setPlayhead(ts) { this.#head = ts; this.draw(); }
  setSrcNames(map)    { this.#srcNames = map; }
  get labelWidth() {
    const W = this.#c?.clientWidth || this.#SRC_W;
    return Math.min(this.#SRC_W, Math.max(52, W * 0.28));
  }

  setData(segs, evts) {
    this.#segs = segs;
    this.#evts = evts;
    this.draw();
  }

  setClipRange(range) {
    this.#clip = range;
    this.draw();
  }

  extendBack(hours) {
    this.#from -= hours * 3600;
    this.draw();
    return this.#from;
  }

  tsToX(ts) { return this.#tsToX(ts); }
  xToTs(x) { return this.#xToTs(x); }

  clipHit(x, y) {
    if (!this.#clip?.active) return null;
    const W = this.#c.clientWidth, H = this.#c.clientHeight;
    if (x < this.labelWidth || x > W || y < 0 || y > H - 18) return null;
    const lane = this.#laneForSource(this.#clip.source_id);
    if (!lane || y < lane.top || y > lane.bot) return null;
    const x1 = this.#tsToX(this.#clip.start);
    const x2 = this.#tsToX(this.#clip.end);
    const minX = Math.min(x1, x2), maxX = Math.max(x1, x2);
    const HANDLE = window.matchMedia?.("(pointer: coarse)").matches ? 16 : 9;
    if (Math.abs(x - minX) <= HANDLE) return { part: "start" };
    if (Math.abs(x - maxX) <= HANDLE) return { part: "end" };
    if (x > minX && x < maxX) return { part: "move", ts: this.#xToTs(x) };
    return null;
  }

  // ── Pure decode — returns null or {ts, srcId, snapEvent} ─
  decode(x, y) {
    const W = this.#c.clientWidth, H = this.#c.clientHeight;
    if (x < this.labelWidth || x > W || y < 0 || y > H - 18) return null;

    const srcIds = this.#uniqueSrcs();
    if (!srcIds.length) return null;
    const laneH = Math.max(1, (H - 18) / srcIds.length);
    const row = Math.min(srcIds.length - 1, Math.max(0, Math.floor(y / laneH)));
    const srcId = srcIds[row];
    const ts = this.#xToTs(x);

    const SNAP = 8;
    let snapEvent = null, best = Infinity;
    for (const e of this.#evts.filter(e => e.source_id === srcId)) {
      const ex = this.#tsToX(e.abs_ts);
      const dist = Math.abs(ex - x);
      if (dist < SNAP && dist < best) { best = dist; snapEvent = e; }
    }

    return { ts, srcId, snapEvent };
  }

  // ── Renderer ──────────────────────────────────────────
  draw() {
    const c = this.#c, ctx = this.#ctx;
    const dpr = window.devicePixelRatio || 1;
    const W = c.clientWidth, H = c.clientHeight;
    c.width = W * dpr; c.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    if (!W || !H) return;
    const span = this.#to - this.#from;
    if (span <= 0) return;

    const srcIds = this.#uniqueSrcs();
    const SRC_W = this.labelWidth;
    const LABEL_H = 18;
    const nowTs = Date.now() / 1000;
    const plotW = Math.max(1, W - SRC_W);

    ctx.fillStyle = "rgba(255,255,255,0.025)";
    ctx.fillRect(SRC_W, 0, plotW, H - LABEL_H);

    const tzOffsetSec = new Date().getTimezoneOffset() * -60;
    let midnightTs = Math.ceil((this.#from - tzOffsetSec) / 86400) * 86400 + tzOffsetSec;
    while (midnightTs <= this.#to) {
      const x = this.#tsToX(midnightTs);
      if (x > SRC_W && x < W) {
        ctx.save();
        ctx.setLineDash([2, 5]);
        ctx.strokeStyle = "rgba(255,255,255,0.16)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, H - LABEL_H);
        ctx.stroke();
        ctx.restore();
      }
      midnightTs += 86400;
    }

    if (srcIds.length) {
      const laneH = (H - LABEL_H) / srcIds.length;
      srcIds.forEach((srcId, row) => {
        const top = row * laneH + 2;
        const bot = top + laneH - 4;
        const mid = (top + bot) / 2;
        const laneLabel = this.#srcNames[srcId] || srcId;

        ctx.fillStyle = "rgba(166,174,190,0.88)";
        ctx.font = "600 9px 'IBM Plex Mono',monospace";
        ctx.textAlign = "right";
        ctx.textBaseline = "middle";
        ctx.fillText(laneLabel.slice(0, 12), SRC_W - 8, mid);

        if (row > 0) {
          ctx.fillStyle = "rgba(255,255,255,0.06)";
          ctx.fillRect(SRC_W, top - 2, plotW, 1);
        }

        ctx.fillStyle = "rgba(255,255,255,0.075)";
        this.#segs.filter(s => s.source_id === srcId).forEach(s => {
          const x1 = Math.max(SRC_W, this.#tsToX(s.start_ts));
          const x2 = Math.min(W, this.#tsToX(s.end_ts ?? Math.min(this.#to, nowTs)));
          if (x2 <= SRC_W || x1 >= W || x2 <= x1) return;
          ctx.fillRect(x1, top + 3, x2 - x1, Math.max(2, bot - top - 6));
        });

        this.#evts.filter(e => e.source_id === srcId).forEach(e => {
          const x = this.#tsToX(e.abs_ts);
          if (x < SRC_W || x > W) return;
          ctx.fillStyle = classColor(e.class);
          ctx.globalAlpha = e.provisional ? 1 : 0.95;
          ctx.fillRect(x - 1, top + 3, 2, Math.max(3, bot - top - 6));
          ctx.globalAlpha = 0.22;
          ctx.fillRect(x - 3, top + 3, 6, Math.max(3, bot - top - 6));
          ctx.globalAlpha = 1;
        });

        if (this.#clip?.active && this.#clip.source_id === srcId) {
          const x1 = Math.max(SRC_W, Math.min(W, this.#tsToX(this.#clip.start)));
          const x2 = Math.max(SRC_W, Math.min(W, this.#tsToX(this.#clip.end)));
          const left = Math.min(x1, x2);
          const right = Math.max(x1, x2);
          if (right > SRC_W && left < W && right - left >= 1) {
            const y1 = top + 1;
            const h = Math.max(8, bot - top - 2);
            ctx.fillStyle = "rgba(232, 165, 88, 0.22)";
            ctx.fillRect(left, y1, right - left, h);
            ctx.strokeStyle = "rgba(232, 165, 88, 0.9)";
            ctx.lineWidth = 1;
            ctx.strokeRect(left + 0.5, y1 + 0.5, Math.max(0, right - left - 1), Math.max(0, h - 1));
            [left, right].forEach(x => {
              ctx.fillStyle = "#e8a558";
              ctx.fillRect(x - 3, y1 - 1, 6, h + 2);
              ctx.fillStyle = "rgba(8, 10, 14, 0.8)";
              ctx.fillRect(x - 1, y1 + 5, 2, Math.max(1, h - 10));
            });
            if (right - left > 58) {
              const label = formatClipDuration(this.#clip.end - this.#clip.start);
              ctx.font = "600 10px 'IBM Plex Mono',monospace";
              ctx.textAlign = "center";
              ctx.textBaseline = "middle";
              const cx = (left + right) / 2;
              const tw = ctx.measureText(label).width + 10;
              ctx.fillStyle = "rgba(8, 10, 14, 0.82)";
              ctx.fillRect(cx - tw / 2, mid - 9, tw, 18);
              ctx.fillStyle = "#f3b46a";
              ctx.fillText(label, cx, mid);
            }
          }
        }

        const srcEvts = this.#evts.filter(e => e.source_id === srcId);
        const nBefore = srcEvts.filter(e => e.abs_ts < this.#from).length;
        const nAfter = srcEvts.filter(e => e.abs_ts > this.#to).length;
        ctx.font = "600 11px 'IBM Plex Mono',monospace";
        ctx.textBaseline = "middle";
        if (nBefore > 0) {
          const label = `◄ ${nBefore}`;
          const tw = ctx.measureText(label).width + 10;
          ctx.fillStyle = "rgba(8,10,14,0.85)";
          ctx.fillRect(SRC_W, mid - 10, tw, 20);
          ctx.fillStyle = "rgba(230,235,244,0.95)";
          ctx.textAlign = "left";
          ctx.fillText(label, SRC_W + 5, mid);
        }
        if (nAfter > 0) {
          const label = `${nAfter} ►`;
          const tw = ctx.measureText(label).width + 10;
          ctx.fillStyle = "rgba(8,10,14,0.85)";
          ctx.fillRect(W - tw, mid - 10, tw, 20);
          ctx.fillStyle = "rgba(230,235,244,0.95)";
          ctx.textAlign = "right";
          ctx.fillText(label, W - 5, mid);
        }
      });
    }

    ctx.font = "400 9px 'IBM Plex Mono',monospace";
    ctx.textBaseline = "alphabetic";
    const axis = planTimeAxis(this.#from, this.#to, SRC_W, W, ctx, {
      minLabelPx: span > 7 * 86400 ? 92 : 78,
      minGridPx: 54,
    });
    axis.grid.forEach(tick => {
      if (tick.x < SRC_W || tick.x > W) return;
      ctx.fillStyle = tick.major ? "rgba(255,255,255,0.12)" : "rgba(255,255,255,0.055)";
      ctx.fillRect(tick.x, 0, 1, H - LABEL_H);
    });
    axis.labels.forEach(tick => {
      if (tick.x < SRC_W || tick.x > W) return;
      ctx.fillStyle = tick.major ? "rgba(230,235,244,0.9)" : "rgba(166,174,190,0.78)";
      ctx.textAlign = "center";
      ctx.fillText(tick.label, tick.x, H - 4);
    });

    const fromDate = new Date(this.#from * 1000);
    const toDate = new Date(this.#to * 1000);
    const fromDay = fromDate.toLocaleDateString(undefined, { day:"numeric", month:"short" });
    const toDay = toDate.toLocaleDateString(undefined, { day:"numeric", month:"short" });
    const dateLabel = fromDay === toDay ? fromDay : `${fromDay} - ${toDay}`;
    ctx.font = "600 9px 'IBM Plex Mono',monospace";
    const dateW = ctx.measureText(dateLabel).width + 10;
    ctx.fillStyle = "rgba(8,10,14,0.82)";
    ctx.fillRect(SRC_W + 2, 2, dateW, 15);
    ctx.fillStyle = "rgba(230,235,244,0.9)";
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
    ctx.fillText(dateLabel, SRC_W + 7, 13);

    // Events loaded: one green tick per loaded interval
    const BAR_Y = H - LABEL_H - 2;
    for (const r of this.#eventsRanges) {
      const ex1 = Math.max(SRC_W, this.#tsToX(r.from));
      const ex2 = Math.min(W, this.#tsToX(r.to));
      if (ex2 > ex1) {
        ctx.fillStyle = "rgba(78,201,138,0.5)";
        ctx.fillRect(ex1, BAR_Y, ex2 - ex1, 2);
      }
    }
    // In-flight fetch: pulsing amber bar
    if (this.#fetchTo > this.#fetchFrom) {
      const fx1 = Math.max(SRC_W, this.#tsToX(this.#fetchFrom));
      const fx2 = Math.min(W, this.#tsToX(this.#fetchTo));
      if (fx2 > fx1) {
        const pulse = 0.35 + 0.3 * Math.sin(Date.now() / 280);
        ctx.fillStyle = `rgba(232,165,88,${pulse.toFixed(2)})`;
        ctx.fillRect(fx1, BAR_Y, fx2 - fx1, 2);
      }
    }
    // Missing footage: gaps between segments in historical window (>5min ago)
    const gapCutoff = nowTs - 300;
    if (this.#segs.length > 0 && this.#from < gapCutoff) {
      const sorted = [...this.#segs].sort((a, b) => a.start_ts - b.start_ts);
      let cursor = this.#from;
      for (const s of sorted) {
        if (s.start_ts > cursor + 30 && cursor < gapCutoff) {
          const gx1 = Math.max(SRC_W, this.#tsToX(cursor));
          const gx2 = Math.min(W, this.#tsToX(Math.min(s.start_ts, gapCutoff)));
          if (gx2 > gx1) {
            ctx.fillStyle = "rgba(226,92,76,0.35)";
            ctx.fillRect(gx1, BAR_Y, gx2 - gx1, 2);
          }
        }
        cursor = Math.max(cursor, s.end_ts ?? s.start_ts);
      }
    }

    if (nowTs >= this.#from && nowTs <= this.#to) {
      const nx = this.#tsToX(nowTs);
      ctx.fillStyle = "rgba(255,255,255,0.28)";
      ctx.fillRect(nx, 0, 1, H - LABEL_H);
      ctx.fillStyle = "rgba(230,235,244,0.75)";
      ctx.font = "600 8px 'IBM Plex Mono',monospace";
      ctx.textAlign = "center";
      ctx.fillText("NOW", nx, H - LABEL_H - 4);
    }

    if (this.#head != null) {
      const x = this.#tsToX(this.#head);
      if (x >= SRC_W && x <= W) {
        ctx.fillStyle = "rgba(255,255,255,0.9)";
        ctx.fillRect(x - 1, 0, 2, H - LABEL_H);
        ctx.beginPath();
        ctx.moveTo(x - 5, 0);
        ctx.lineTo(x + 5, 0);
        ctx.lineTo(x, 8);
        ctx.closePath();
        ctx.fill();
      }
    }
  }

  #tsToX(ts) {
    const W = this.#c.clientWidth;
    const SRC_W = this.labelWidth;
    return SRC_W + ((ts - this.#from) / (this.#to - this.#from)) * Math.max(1, W - SRC_W);
  }
  #xToTs(x)  {
    const W = this.#c.clientWidth;
    const SRC_W = this.labelWidth;
    return this.#from + ((x - SRC_W) / Math.max(1, W - SRC_W)) * (this.#to - this.#from);
  }
  #uniqueSrcs() {
    const ids = [];
    const add = id => { if (id && !ids.includes(id)) ids.push(id); };
    Object.keys(this.#srcNames).forEach(add);
    this.#segs.forEach(s => add(s.source_id));
    this.#evts.forEach(e => add(e.source_id));
    const visible = new Set([
      ...this.#segs.map(s => s.source_id),
      ...this.#evts.map(e => e.source_id),
    ]);
    return ids.filter(id => visible.has(id));
  }
  #laneForSource(sourceId) {
    const srcIds = this.#uniqueSrcs();
    const idx = srcIds.indexOf(sourceId);
    if (idx < 0) return null;
    const H = this.#c.clientHeight;
    const LABEL_H = 18;
    const laneH = (H - LABEL_H) / Math.max(1, srcIds.length);
    const top = idx * laneH + 2;
    const bot = top + laneH - 4;
    return { top, bot, mid: (top + bot) / 2 };
  }
}

// ═══════════════════════════════════════════════════════
// App — thin wiring layer
// ═══════════════════════════════════════════════════════
const V2_SPEEDS    = [{label:"0.5×",rate:.5},{label:"1×",rate:1},{label:"2×",rate:2},{label:"4×",rate:4}];
const POST_BUFFER  = 10;
const LIVE_OPEN_MAX_AGE = 3600;
const LIVE_DVR_TOLERANCE_SECONDS = 1.5;
const LIVE_DVR_EDGE_PAD_SECONDS = 0.25;
const LIVE_TIMELINE_FUTURE_PAD_SECONDS = 600;
const LIVE_EDGE_RATE_RESET_SECONDS = 4;
const LIVE_DET_WINDOW_SECONDS = 6;
const LIVE_DET_POLL_MS = 500;
const LIVE_DET_SYNC_TOLERANCE_SECONDS = 0.20;
const LIVE_DET_SYNC_MAX_AGE_SECONDS = 12;
const ABSOLUTE_SEEK_RETRIES = 6;
const ABSOLUTE_SEEK_RETRY_MS = 750;
const ZONE_ALL = "all";
const ZONE_NONE = "none";
const CLIP_DEFAULT_BEFORE = 30;
const CLIP_DEFAULT_AFTER = 30;
const CLIP_MIN_DURATION = 1;
const CLIP_MAX_DURATION = 10 * 60;

// ── DOM ───────────────────────────────────────────────
const $ = id => document.getElementById(id);
const el = {
  video:   $("v2Video"),
  liveVideo:$("v2LiveVideo"),
  canvas:  $("v2BoxCanvas"),
  zoneCanvas:$("v2ZoneCanvas"),
  tlCanvas:$("v2Timeline"),
  thumb:   $("v2ThumbPreview"),
  empty:   $("v2Empty"),
  emptyText: $("v2EmptyText"),
  emptyCta: $("v2EmptyCta"),
  tsDisp:  $("v2Timestamp"),
  tsTime:  $("v2TsTime"),
  tsDate:  $("v2TsDate"),
  srcCtrl: $("v2SourceCtrl"),
  clsField:$("v2ClassField"),
  clsCtrl: $("v2ClassCtrl"),
  nearScope:$("v2NearScope"),
  eventThumbs:$("v2EventThumbs"),
  eventCount:$("v2EventCount"),
  play:    $("v2Play"),
  prev:    $("v2Prev"),
  next:    $("v2Next"),
  rewind:  $("v2Rewind"),
  speeds:  $("v2Speeds"),
  loop:    $("v2Loop"),
  timeDisp:$("v2TimeDisp"),
  boxes:   $("v2Boxes"),
  zones:   $("v2Zones"),
  viewer:  document.querySelector(".v2-viewer"),
  activityToggle: $("v2ActivityToggle"),
  activityBackdrop: $("v2ActivityBackdrop"),
  activityPanel: $("v2ActivityPanel"),
  notifyToggle: $("v2NotificationsToggle"),
  notifyBadge: $("v2NotificationsBadge"),
  notifyPanel: $("v2NotificationsPanel"),
  notifySub: $("v2NotificationsSub"),
  notifyList: $("v2NotificationsList"),
  notifyReadAll: $("v2NotificationsReadAll"),
  zoneBar: $("v2ZoneBar"),
  zoneName: $("v2ZoneName"),
  zoneTriggerLabel: $("v2ZoneTriggerLabel"),
  zoneMenu: $("v2ZoneMenu"),
  zonePicker: $("v2ZonePicker"),
  zoneCount:$("v2ZoneCount"),
  zonePrev:$("v2ZonePrev"),
  zoneNext:$("v2ZoneNext"),
  zoneNew:$("v2ZoneNew"),
  zoneDelete:$("v2ZoneDelete"),
  zoneSave:$("v2ZoneSave"),
  zoneReset:$("v2ZoneReset"),
  zoneCancel:$("v2ZoneCancel"),
  fullscreen:$("v2Fullscreen"),
  download: $("v2DownloadClip"),
  downloadFrame: $("v2DownloadFrame"),
  clipToolbar: $("v2ClipToolbar"),
  clipRange: $("v2ClipRange"),
  clipPreview: $("v2ClipPreview"),
  clipDownload: $("v2ClipDownload"),
  clipCancel: $("v2ClipCancel"),
  status:  $("v2Status"),
  liveBtn: $("v2LiveBtn"),
  stage:   document.querySelector(".v2-stage"),
  ruler:   $("v2Ruler"),
};

// ── Core instances ────────────────────────────────────
const player   = new V2Player(el.video);
const timeline = new V2Timeline(el.tlCanvas);
const mode     = new AppMode(player);
mode.onModeChange = next => {
  if (next !== "playlist" && st?.clip?.previewing) {
    setClipPreviewing(false);
    if (next === "seek") setStatus("REPLAY");
  }
};

// ── App state ─────────────────────────────────────────
const st = {
  segments: [],
  events:   [],
  classes:  {},
  sources:  [],
  sourceStatus: {},
  zones: [],
  zonesSource: null,
  activeZoneId: null,
  zoneEdit: {
    active: false,
    zones: [],
    selected: -1,
    dragPoint: null,
    dragPoly: false,
    last: null,
  },
  clip: {
    active: false,
    toolbarOpen: false,
    downloading: false,
    previewing: false,
    previewSeq: 0,
    previewRestarting: false,
    downloadTimer: null,
    start: null,
    end: null,
    sourceId: null,
    drag: null,
  },
  source:   "all",
  cls:      new Set(),   // included classes (amber)
  xls:      new Set(),   // excluded classes (red)
  window:        { from: 0, to: 0 },
  segmentBounds: null,
  eventsLoaded:  { ranges: [] },  // list of {from,to} intervals, merged on insert
  speed:    parseInt(localStorage.getItem("v2speed") || "1"),
  loop:     true,
  showBoxes:localStorage.getItem("v2boxes") !== "0",
  overlays: { sourceId: null, from: 0, to: 0, detections: [], loadingKey: null, seq: 0 },
  initDone: false,
  classSearchSeq: 0,
  summary: { total: 0, classes: {} },
  activityOpen: false,
  notificationsOpen: false,
  notifications: [],
  unreadNotifications: 0,
};
const EVENTS_BUFFER = 3 * 3600;   // load 3h extra on each side of visible window
const SEGMENTS_BUFFER = 3600;     // timeline media coverage around visible window
const NOTIFICATION_POLL_MS = 1000;
let notificationPollTimer = null;
let notificationPollInFlight = false;
let notificationPollNeedsRefresh = false;
let notificationPollSeq = 0;
let notificationPollStarted = false;
let absoluteSeekSeq = 0;

function selectedZoneParam() {
  return st.activeZoneId == null ? ZONE_NONE : String(st.activeZoneId);
}

function appendZoneParam(params) {
  params.set("zone", selectedZoneParam());
  return params;
}

function _eventsRangesClear()   { st.eventsLoaded.ranges = []; }
function _eventsRangesAdd(from, to) {
  const r = st.eventsLoaded.ranges;
  r.push({ from, to });
  r.sort((a, b) => a.from - b.from);
  const merged = [r[0]];
  for (let i = 1; i < r.length; i++) {
    const last = merged[merged.length - 1];
    if (r[i].from <= last.to) last.to = Math.max(last.to, r[i].to);
    else merged.push(r[i]);
  }
  st.eventsLoaded.ranges = merged;
}
function _eventsRangesCovers(from, to) {
  for (const r of st.eventsLoaded.ranges) {
    if (r.from <= from && r.to >= to) return true;
  }
  return false;
}
function _eventsLoadedBounds() {
  const r = st.eventsLoaded.ranges;
  return r.length ? { from: r[0].from, to: r[r.length - 1].to } : { from: 0, to: 0 };
}

const liveTail = {
  hls: null,
  active: false,
  srcId: null,
  starting: false,
  token: 0,
  pollTimer: null,
  clockTimer: null,
  cancelFrame: null,   // teardown for the per-frame overlay loop (rVFC/rAF)
  latestDet: null,
  recentDets: [],   // recent detections buffer (sorted by abs_ts asc)
  recentSeq: 0,     // bumped when recentDets is replaced → tracklet cache key
  _tracklets: null,
  _trackletsSeq: -1,
  syncToDetections: false,
  syncDetTs: null,
  holdingForDetections: false,
  window: null,
  bitcTimeOffset: null,
  targetTs: null,
};

function urlTimestamp(ts) {
  const n = Number(ts);
  // Reject null/undefined (Number(null)===0) and any non-positive value: a URL ts
  // is an absolute BITC/Unix time, never 0. Prevents ?ts=0 from a live click.
  if (ts == null || !Number.isFinite(n) || n <= 0) return null;
  return n.toFixed(3).replace(/\.?0+$/, "");
}

// ── Derived views ─────────────────────────────────────
// All segments for source — used for timeline bands (always show coverage)
function allSegsForSrc() {
  return st.source === "all" ? st.segments : st.segments.filter(s => s.source_id === st.source);
}

function filteredSegs() {
  let s = st.segments;
  if (st.source !== "all") s = s.filter(x => x.source_id === st.source);
  return s;
}

function filteredEvts() {
  let e = st.events;
  if (st.source !== "all") e = e.filter(x => x.source_id === st.source);
  if (st.xls.size > 0)     e = e.filter(x => !st.xls.has(x["class"]));
  if (st.cls.size > 0)     e = e.filter(x => st.cls.has(x["class"]));
  return e;
}

// ── Nearby event widget ───────────────────────────────
const NEAR_EVENT_LIMIT = 10;
const NEAR_EVENT_REFRESH_MS = 1500;

function nearbyClassSet() {
  if (st.cls.size > 0) return new Set(st.cls);
  // Default to person, but respect exclusions
  const def = new Set(["person"]);
  st.xls.forEach(c => def.delete(c));
  return def.size > 0 ? def : new Set(["person"]);
}

function nearbyScopeLabel() {
  const classes = [...nearbyClassSet()];
  return classes.length ? classes.join(", ") : "all";
}

function renderNearScope() {
  if (el.nearScope) el.nearScope.textContent = nearbyScopeLabel();
}

function nearestEvents(baseTs) {
  let evts = st.events;
  if (st.source !== "all") evts = evts.filter(e => e.source_id === st.source);
  const classes = nearbyClassSet();
  if (classes.size > 0) evts = evts.filter(e => classes.has(e.class));
  return evts
    .map(e => ({ event: e, dist: Math.abs(e.abs_ts - baseTs) }))
    .sort((a, b) => a.dist - b.dist || a.event.abs_ts - b.event.abs_ts)
    .slice(0, NEAR_EVENT_LIMIT)
    .map(x => x.event)
    .sort((a, b) => a.abs_ts - b.abs_ts);  // stable display order — prevents sig churn
}

function classFilteredEvents(classes = st.cls) {
  let evts = st.events;
  if (st.source !== "all") evts = evts.filter(e => e.source_id === st.source);
  if (classes.size > 0) evts = evts.filter(e => classes.has(e.class));
  return evts;
}

function setStatus(state, detail = "") {
  if (!el.status) return;
  const textEl = el.status.querySelector("[data-status-text]") || el.status;
  const normalized = String(state || "replay").toLowerCase();
  let cls = "replay";
  let text = "REPLAY";
  if (normalized === "live") {
    cls = "live";
    text = detail || "LIVE";
  } else if (normalized === "buffering" || normalized === "sync" || normalized === "search") {
    cls = "buffering";
    text = normalized === "search" ? "SEARCHING" : "BUFFERING...";
  } else if (normalized === "offline" || normalized === "live err" || normalized === "none") {
    cls = "offline";
    text = normalized === "none" ? "NO EVENTS" : (detail || "OFFLINE");
  } else if (normalized === "auto" || normalized === "seek" || normalized === "playlist" || normalized === "replay") {
    cls = "replay";
    text = detail || "REPLAY";
  } else {
    text = String(state).toUpperCase();
  }
  el.status.classList.remove("live", "buffering", "offline", "replay");
  el.status.classList.add(cls);
  textEl.textContent = text;
}

function setPlayIcon(playing) {
  if (!el.play) return;
  el.play.innerHTML = playing
    ? '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true"><rect x="3" y="2.5" width="2" height="7" rx=".4" fill="currentColor"/><rect x="7" y="2.5" width="2" height="7" rx=".4" fill="currentColor"/></svg>'
    : '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true"><path d="M3.5 2.5 9.5 6l-6 3.5v-7z" fill="currentColor"/></svg>';
  el.play.classList.toggle("playing", playing);
}

function formatClock(ts) {
  return new Date(ts * 1000).toLocaleTimeString(undefined,
    { hour:"2-digit", minute:"2-digit", second:"2-digit" });
}

function formatClipDuration(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${String(r).padStart(2, "0")}`;
}

function formatDateChip(ts) {
  const d = new Date(ts * 1000);
  const date = d.toLocaleDateString(undefined, { year:"numeric", month:"2-digit", day:"2-digit" });
  const weekday = d.toLocaleDateString(undefined, { weekday:"short" });
  return `${date} · ${weekday}`;
}

function setTimestampChip(ts, srcId = null, live = false) {
  if (ts == null) return;
  if (el.tsTime) el.tsTime.textContent = formatClock(ts);
  if (el.tsDate) el.tsDate.textContent = formatDateChip(ts);
  if (el.timeDisp) {
    el.timeDisp.innerHTML = `<span>${formatClock(ts)}</span><span class="sub">/ 23:59:59</span>`;
  }
  if (el.tsDisp) {
    el.tsDisp.dataset.sourceId = srcId || "";
    el.tsDisp.dataset.live = live ? "1" : "0";
  }
}

function renderRuler() {
  if (!el.ruler) return;
  el.ruler.querySelectorAll(".tick").forEach(n => n.remove());
  const grid = el.ruler.querySelector(".grid");
  if (grid) {
    grid.style.left = `${timeline.labelWidth}px`;
    grid.style.right = "16px";
  }
  const span = st.window.to - st.window.from;
  if (span <= 0) return;
  const width = el.ruler.clientWidth || 1;
  const labelW = timeline.labelWidth;
  const ctx = renderRuler._measure ??= document.createElement("canvas").getContext("2d");
  ctx.font = "400 10px 'IBM Plex Mono', monospace";
  const axis = planTimeAxis(st.window.from, st.window.to, labelW, width - 16, ctx, {
    minLabelPx: span > 7 * 86400 ? 96 : 74,
    minGridPx: 48,
  });
  axis.labels.forEach(mark => {
    const tick = document.createElement("span");
    tick.className = "tick" + (mark.major ? " major" : "");
    tick.style.left = `${mark.x}px`;
    tick.textContent = mark.label;
    el.ruler.appendChild(tick);
  });
}

function setTimelineWindow(from, to) {
  st.window.from = from;
  st.window.to = to;
  timeline.setWindow(from, to);
  renderRuler();
  syncClipSelection();
  // Events only exist in the past — cap check at now so the live-gap
  // (window.to = nowTs + LIVE_TIMELINE_FUTURE_PAD_SECONDS) never triggers perpetual re-loads.
  const nowTs = Date.now() / 1000;
  const checkTo = Math.min(to, nowTs);
  const needsLoad = checkTo > from && !_eventsRangesCovers(from, checkTo);
  if (needsLoad) {
    clearTimeout(_fetchDebounce);
    _fetchDebounce = setTimeout(() => load(), 350);
  }
}

function centerWindowOn(ts) {
  const span = st.window.to - st.window.from;
  st.window.from = ts - span * 0.4;
  st.window.to   = ts + span * 0.6;
  setTimelineWindow(st.window.from, st.window.to);
}

async function fetchNearestEvents(classes, around, limit = 20) {
  const p = new URLSearchParams();
  if (st.source !== "all") p.set("source", st.source);
  if (classes.size > 0) p.set("classes", [...classes].join(","));
  appendZoneParam(p);
  p.set("around", Math.floor(around));
  p.set("limit", String(limit));
  const r = await fetch(`/api/video/events?${p}`, { cache:"no-store" });
  if (!r.ok) return [];
  const data = await r.json();
  return data.events || [];
}

function todayRange() {
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  const end = new Date(start);
  end.setDate(end.getDate() + 1);
  return { since: start.getTime() / 1000, until: end.getTime() / 1000 };
}

async function fetchActivitySummary() {
  const { since, until } = todayRange();
  const p = new URLSearchParams({ since: String(Math.floor(since)), until: String(Math.ceil(until)) });
  if (st.source !== "all") p.set("source", st.source);
  appendZoneParam(p);
  const r = await fetch(`/api/video/activity-summary?${p}`, { cache:"no-store" }).catch(() => null);
  if (!r?.ok) {
    const classes = {};
    let evts = st.events;
    if (st.source !== "all") evts = evts.filter(e => e.source_id === st.source);
    evts.forEach(e => { classes[e.class] = (classes[e.class] || 0) + 1; });
    st.summary = { total: Object.values(classes).reduce((a, b) => a + b, 0), classes };
    return;
  }
  st.summary = await r.json();
}

async function fetchZonesForSource() {
  const p = new URLSearchParams();
  if (st.source !== "all") p.set("source", st.source);
  const r = await fetch(`/api/video/zones?${p}`, { cache:"no-store" }).catch(() => null);
  if (!r?.ok) return { zones: [] };
  return r.json();
}

function updateActivityCount() {
  if (!el.eventCount) return;
  const n = st.summary.total || 0;
  el.eventCount.textContent = `${n} today`;
  if (el.activityToggle) {
    el.activityToggle.title = `Activity (${n} today)`;
    el.activityToggle.setAttribute("aria-label", st.activityOpen ? "Close activity" : `Open activity (${n} today)`);
  }
}

const activityDrawerMq = window.matchMedia?.("(max-width: 760px), (max-height: 480px)");

function isActivityDrawerLayout() {
  return !!activityDrawerMq?.matches;
}

function setActivityOpen(open) {
  const next = !!open && isActivityDrawerLayout();
  st.activityOpen = next;
  el.viewer?.classList.toggle("activity-open", next);
  el.activityToggle?.setAttribute("aria-expanded", next ? "true" : "false");
  if (el.activityToggle) {
    const n = st.summary.total || 0;
    el.activityToggle.setAttribute("aria-label", next ? "Close activity" : `Open activity (${n} today)`);
  }
  if (el.activityBackdrop) el.activityBackdrop.hidden = !next;
  if (el.activityPanel) {
    el.activityPanel.setAttribute("aria-hidden", isActivityDrawerLayout() && !next ? "true" : "false");
  }
}

function syncActivityDrawerMode() {
  setActivityOpen(st.activityOpen);
}

function closeActivityAfterMobilePick() {
  if (isActivityDrawerLayout()) setActivityOpen(false);
}

async function fetchSourceStatus() {
  const r = await fetch("/api/video/source-status", { cache:"no-store" }).catch(() => null);
  if (!r?.ok) return;
  const data = await r.json();
  st.sourceStatus = data.sources || {};
}

function sourceState(srcId) {
  if (srcId === "all") {
    const states = Object.values(st.sourceStatus).map(s => s.state);
    if (states.includes("live")) return "live";
    if (states.includes("buffering")) return "buffering";
    return states.length ? "offline" : "buffering";
  }
  return st.sourceStatus[srcId]?.state || "buffering";
}

async function fetchLiveWindow(srcId) {
  const p = new URLSearchParams({ source: srcId });
  const r = await fetch(`/api/video/live-window?${p}`, { cache:"no-store" }).catch(() => null);
  if (!r?.ok) return null;
  const data = await r.json().catch(() => ({}));
  return data.window || null;
}

async function fetchNativeLiveSource(srcId) {
  const p = new URLSearchParams({ source: srcId });
  const r = await fetch(`/api/video/native-live?${p}`, { cache:"no-store" }).catch(() => null);
  if (!r?.ok) return null;
  const data = await r.json().catch(() => ({}));
  return data.native || null;
}

function liveWindowContains(win, ts) {
  return Boolean(win
    && ts >= win.start_ts - LIVE_DVR_TOLERANCE_SECONDS
    && ts <= win.end_ts + LIVE_DVR_TOLERANCE_SECONDS);
}

function liveMediaOffsetForTs(win, ts) {
  const duration = Math.max(0, win.end_ts - win.start_ts);
  return Math.max(0, Math.min(Math.max(0, duration - LIVE_DVR_EDGE_PAD_SECONDS), ts - win.start_ts));
}

function liveSeekableTargetForTs(win, ts) {
  const offset = liveMediaOffsetForTs(win, ts);
  const ranges = el.liveVideo.seekable;
  if (ranges?.length) {
    const start = ranges.start(0);
    const end = ranges.end(ranges.length - 1);
    if (Number.isFinite(start) && Number.isFinite(end) && end > start) {
      return Math.max(start, Math.min(Math.max(start, end - LIVE_DVR_EDGE_PAD_SECONDS), start + offset));
    }
  }
  return offset;
}

function seekLiveVideoToTs(ts, win) {
  if (!win || ts == null) return false;
  const target = liveSeekableTargetForTs(win, ts);
  try {
    el.liveVideo.currentTime = target;
  } catch {
    return false;
  }
  liveTail.window = win;
  liveTail.targetTs = ts;
  liveTail.bitcTimeOffset = ts - target;
  return true;
}

function seekLiveVideoToMarkerTs(ts, tolerance = LIVE_DET_SYNC_TOLERANCE_SECONDS, opts = {}) {
  if (ts == null || !Number.isFinite(Number(ts))) return false;
  const markerTs = decodeLiveMarker(el.liveVideo);
  if (markerTs == null) return false;
  const delta = Number(ts) - markerTs;
  if (Math.abs(delta) <= tolerance) return true;
  if (delta < 0 && opts.allowBackward === false) return false;
  let target = (el.liveVideo.currentTime || 0) + delta;
  const ranges = el.liveVideo.seekable;
  if (ranges?.length) {
    const start = ranges.start(0);
    const end = ranges.end(ranges.length - 1);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return false;
    if (target < start - tolerance || target > end + tolerance) return false;
    target = Math.max(start, Math.min(end, target));
  }
  try {
    el.liveVideo.currentTime = target;
  } catch {
    return false;
  }
  liveTail.targetTs = Number(ts);
  liveTail.bitcTimeOffset = Number(ts) - target;
  return true;
}

function latestLiveBoxDetection() {
  const dets = (liveTail.recentDets || [])
    .filter(d => d.source_id === liveTail.srcId && (d.boxes || []).length)
    .sort((a, b) => Number(a.abs_ts) - Number(b.abs_ts));
  if (dets.length) return dets[dets.length - 1];
  return (liveTail.latestDet?.source_id === liveTail.srcId && (liveTail.latestDet.boxes || []).length)
    ? liveTail.latestDet
    : null;
}

function liveDetectionTargetTs() {
  const det = latestLiveBoxDetection();
  if (!det || !Number.isFinite(Number(det.abs_ts))) return null;
  const ts = Number(det.abs_ts);
  const age = Date.now() / 1000 - ts;
  if (age > LIVE_DET_SYNC_MAX_AGE_SECONDS) return liveTail.syncDetTs;
  if (liveTail.syncDetTs == null || ts > liveTail.syncDetTs) {
    liveTail.syncDetTs = ts;
  }
  return liveTail.syncDetTs;
}

function syncLiveVideoToLatestBoxes() {
  if (!liveTail.active || !liveTail.syncToDetections) return false;
  const targetTs = liveDetectionTargetTs();
  if (targetTs == null) return false;
  const markerTs = decodeLiveMarker(el.liveVideo);
  if (markerTs == null) return false;
  const delta = targetTs - markerTs;
  if (delta > LIVE_DET_SYNC_TOLERANCE_SECONDS) {
    seekLiveVideoToMarkerTs(targetTs, LIVE_DET_SYNC_TOLERANCE_SECONDS / 2, {
      allowBackward: false,
    });
  }
  liveTail.holdingForDetections = true;
  if (!el.liveVideo.paused) el.liveVideo.pause();
  return Math.abs(delta) <= LIVE_DET_SYNC_TOLERANCE_SECONDS;
}

function liveTailCurrentTs() {
  const markerTs = decodeLiveMarker(el.liveVideo);
  if (markerTs != null) return markerTs;
  if (liveTail.bitcTimeOffset != null) {
    return liveTail.bitcTimeOffset + (el.liveVideo.currentTime || 0);
  }
  return liveTail.latestDet?.abs_ts ?? Date.now() / 1000;
}

function selectedPlaybackRate() {
  return V2_SPEEDS[st.speed]?.rate ?? 1;
}

function setLivePlaybackRate(rate) {
  if (!el.liveVideo) return;
  if (Math.abs((el.liveVideo.playbackRate || 1) - rate) < 0.01) return;
  el.liveVideo.defaultPlaybackRate = rate;
  el.liveVideo.playbackRate = rate;
}

function updateLivePlaybackRate(ts = liveTailCurrentTs()) {
  if (!liveTail.active) return;
  const selected = selectedPlaybackRate();
  let rate = selected;
  if (selected > 1) {
    const lag = Date.now() / 1000 - ts;
    const isDvrCatchup = liveTail.bitcTimeOffset != null && lag > LIVE_EDGE_RATE_RESET_SECONDS;
    rate = isDvrCatchup ? selected : 1;
  }
  setLivePlaybackRate(rate);
}

async function seekLiveTail(srcId, ts) {
  return startLiveTail(srcId, { seekTs: ts });
}

function mergeEvents(events) {
  if (!events.length) return;
  const byId = new Map(st.events.map(e => [e.id, e]));
  events.forEach(e => byId.set(e.id, e));
  st.events = [...byId.values()].sort((a, b) => b.abs_ts - a.abs_ts);
}

function replaceProvisionalEvents(events, srcId = null) {
  st.events = st.events.filter(e =>
    !e.provisional || (srcId && e.source_id !== srcId)
  );
  mergeEvents(events);
}

// The server sends each segment's honest recorder-open start_ts plus the BITC
// anchor media_epoch. The timeline and player index everything by BITC time, so
// here we set start_ts/end_ts to the segment's BITC coverage
// [media_epoch, media_epoch + duration]. One value (media_epoch) places the file
// in the universe; offsets into it are derived, never stored.
function worldizeSeg(s) {
  const epoch = Number(s.media_epoch);
  if (!Number.isFinite(epoch)) return s;        // unanchored (e.g. open live edge)
  const dur = Number(s.duration_sec);
  return {
    ...s,
    start_ts: epoch,
    end_ts: Number.isFinite(dur) ? epoch + dur : (s.end_ts ?? null),
  };
}

function mergeSegments(segments) {
  if (!segments?.length) return;
  const byId = new Map(st.segments.map(s => [s.id, s]));
  segments.forEach(s => {
    const w = worldizeSeg(s);
    byId.set(w.id, { ...(byId.get(w.id) || {}), ...w });
  });
  st.segments = [...byId.values()].sort((a, b) => b.start_ts - a.start_ts);
  player.setSegments(st.segments);
}

function mergeClassCounts(events) {
  const counts = {};
  events.forEach(e => { counts[e.class] = (counts[e.class] || 0) + 1; });
  Object.entries(counts).forEach(([cls, n]) => {
    st.classes[cls] = Math.max(st.classes[cls] || 0, n);
  });
}

async function handleClassSelectionChanged(classes) {
  const seq = ++st.classSearchSeq;
  stopClipPreview();
  renderClsCtrl();
  renderNearScope();
  timeline.setData(allSegsForSrc(), filteredEvts());
  scheduleNearestEvents(true);

  if (!classes.size) {
    if (!liveTail.active) mode.stop();
    setStatus(liveTail.active ? "LIVE" : "AUTO");
    return;
  }

  const baseTs = player.displayTs ?? Date.now() / 1000;
  let evts = classFilteredEvents(classes);
  if (!evts.length) {
    setStatus("SEARCH");
    evts = await fetchNearestEvents(classes, baseTs, 20);
    if (seq !== st.classSearchSeq) return;
    mergeEvents(evts);
  }

  if (!evts.length) {
    setStatus("NONE");
    setTimeout(() => { if (seq === st.classSearchSeq) setStatus("AUTO"); }, 1800);
    timeline.setData(allSegsForSrc(), filteredEvts());
    scheduleNearestEvents(true);
    return;
  }

  evts = [...evts].sort((a, b) =>
    Math.abs(a.abs_ts - baseTs) - Math.abs(b.abs_ts - baseTs) || a.abs_ts - b.abs_ts
  );
  const target = evts[0];
  if (target.provisional) {
    centerWindowOn(target.abs_ts);
    timeline.setData(allSegsForSrc(), filteredEvts());
    seekToEvent(target);
    return;
  }

  stopLiveTail(false);
  centerWindowOn(target.abs_ts);
  await load();
  if (seq !== st.classSearchSeq) return;

  const playlist = classFilteredEvents(classes).sort((a, b) => a.abs_ts - b.abs_ts);
  const startIdx = Math.max(0, playlist.findIndex(e => e.id === target.id));
  if (playlist.length) mode.playEventPlaylist(playlist, st.loop, startIdx);
  scrollTimelineToTs(target.abs_ts);
  scheduleNearestEvents(true);
  setStatus("AUTO");
}

function relEventLabel(ts, baseTs) {
  const delta = Math.round(ts - baseTs);
  const sign = delta >= 0 ? "+" : "-";
  const abs = Math.abs(delta);
  if (abs < 60) return `${sign}${abs}s`;
  if (abs < 3600) return `${sign}${Math.round(abs / 60)}m`;
  return `${sign}${Math.round(abs / 3600)}h`;
}

function eventLocalTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString(undefined,
    { hour:"2-digit", minute:"2-digit", second:"2-digit" });
}

function sourceLabel(srcId) {
  return st.sources.find(s => s.id === srcId)?.name || srcId;
}

function notificationAge(ts) {
  const delta = Math.max(0, Math.round(Date.now() / 1000 - Number(ts || 0)));
  if (delta < 60) return "now";
  if (delta < 3600) return `${Math.floor(delta / 60)}m`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h`;
  return `${Math.floor(delta / 86400)}d`;
}

function updateNotificationBadge() {
  if (!el.notifyBadge) return;
  const n = st.unreadNotifications || 0;
  el.notifyBadge.hidden = n <= 0;
  el.notifyBadge.textContent = n > 99 ? "99+" : String(n);
}

function setNotificationsOpen(open) {
  const next = !!open;
  st.notificationsOpen = next;
  if (el.notifyPanel) el.notifyPanel.hidden = !next;
  el.notifyToggle?.setAttribute("aria-expanded", next ? "true" : "false");
  el.notifyToggle?.setAttribute("aria-label", next ? "Close notifications" : "Open notifications");
  if (next) requestNotificationRefresh();
}

function scheduleNotificationPoll(delay = NOTIFICATION_POLL_MS) {
  if (notificationPollTimer != null) {
    if (delay > 0) return;
    window.clearTimeout(notificationPollTimer);
  }
  notificationPollTimer = window.setTimeout(() => {
    notificationPollTimer = null;
    pollNotifications();
  }, Math.max(0, delay));
}

function requestNotificationRefresh() {
  notificationPollNeedsRefresh = true;
  if (notificationPollInFlight) return;
  scheduleNotificationPoll(0);
}

function startNotificationPolling() {
  if (notificationPollStarted) return;
  notificationPollStarted = true;
  requestNotificationRefresh();
}

async function pollNotifications() {
  if (notificationPollInFlight) {
    notificationPollNeedsRefresh = true;
    return;
  }
  notificationPollInFlight = true;
  notificationPollNeedsRefresh = false;
  const seq = ++notificationPollSeq;
  try {
    if (st.notificationsOpen) await loadNotifications(seq);
    else await refreshNotificationCount(seq);
  } finally {
    notificationPollInFlight = false;
    scheduleNotificationPoll(notificationPollNeedsRefresh ? 0 : NOTIFICATION_POLL_MS);
  }
}

async function loadNotifications(seq = ++notificationPollSeq) {
  const r = await fetch("/api/notifications?limit=20", { cache:"no-store" }).catch(() => null);
  if (!r?.ok) return;
  const data = await r.json().catch(() => ({}));
  if (seq !== notificationPollSeq) return;
  st.notifications = data.notifications || [];
  st.unreadNotifications = data.unread_count || 0;
  updateNotificationBadge();
  renderNotifications();
}

async function refreshNotificationCount(seq = ++notificationPollSeq) {
  const r = await fetch("/api/notifications/unread-count", { cache:"no-store" }).catch(() => null);
  if (!r?.ok) return;
  const data = await r.json().catch(() => ({}));
  if (seq !== notificationPollSeq) return;
  st.unreadNotifications = data.unread_count || 0;
  updateNotificationBadge();
}

function renderNotifications() {
  if (!el.notifyList) return;
  el.notifyList.innerHTML = "";
  const unread = st.unreadNotifications || 0;
  if (el.notifySub) {
    el.notifySub.textContent = unread
      ? `${unread} unread`
      : `${st.notifications.length} recent`;
  }
  if (el.notifyReadAll) el.notifyReadAll.disabled = unread <= 0;
  if (!st.notifications.length) {
    const empty = document.createElement("div");
    empty.className = "v2-notify-empty";
    empty.textContent = "No notifications";
    el.notifyList.appendChild(empty);
    return;
  }
  st.notifications.forEach(n => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "v2-notify-row" + (n.read ? "" : " unread");

    const thumb = document.createElement("div");
    thumb.className = "v2-notify-thumb";
    if (n.thumb_url) {
      const img = document.createElement("img");
      img.src = n.thumb_url;
      img.alt = "";
      img.loading = "lazy";
      img.addEventListener("error", () => {
        thumb.innerHTML = "";
        const fallback = document.createElement("div");
        fallback.className = "v2-notify-thumb-fallback";
        fallback.textContent = "EVT";
        thumb.appendChild(fallback);
      }, { once: true });
      thumb.appendChild(img);
    } else {
      const fallback = document.createElement("div");
      fallback.className = "v2-notify-thumb-fallback";
      fallback.textContent = "EVT";
      thumb.appendChild(fallback);
    }

    const main = document.createElement("div");
    main.className = "v2-notify-main";

    const title = document.createElement("div");
    title.className = "v2-notify-row-title";
    const name = document.createElement("div");
    name.className = "v2-notify-name";
    name.textContent = n.title || "Notification";
    const time = document.createElement("div");
    time.className = "v2-notify-time";
    time.textContent = notificationAge(n.event_ts || n.created_at);
    title.append(name, time);

    const body = document.createElement("div");
    body.className = "v2-notify-body";
    body.textContent = n.body || n.rule_name || "";

    const meta = document.createElement("div");
    meta.className = "v2-notify-meta";
    meta.textContent = `${sourceLabel(n.source_id)} · ${n.class || "object"}`;

    main.append(title, body, meta);
    row.append(thumb, main);
    row.addEventListener("click", () => openNotification(n));
    el.notifyList.appendChild(row);
  });
}

async function openNotification(notification) {
  if (!notification.read) {
    fetch(`/api/notifications/${notification.id}/read`, { method:"POST" }).catch(() => {});
  }
  const target = notification.target_url || "/";
  const url = new URL(target, location.href);
  if (url.origin === location.origin && url.pathname === location.pathname) {
    const { ts, live } = applyQueryState(url.searchParams);
    st.initDone = true;
    renderSrcCtrl();
    renderClsCtrl();
    renderNearScope();
    setNotificationsOpen(false);
    if (ts != null) centerWindowOn(ts);
    if (live && ts == null) {
      load();
      startLiveTail(st.source !== "all" ? st.source : null);
      return;
    }
    if (ts != null) {
      load();
      await seekToTimestamp(st.source !== "all" ? st.source : null, ts, { scroll: true });
      return;
    }
  }
  location.assign(target);
}

async function markAllNotificationsRead() {
  const r = await fetch("/api/notifications/read-all", { method:"POST" }).catch(() => null);
  if (!r?.ok) return;
  notificationPollSeq += 1;
  st.notifications = st.notifications.map(n => ({ ...n, read: true, read_at: n.read_at || Date.now() / 1000 }));
  st.unreadNotifications = 0;
  updateNotificationBadge();
  renderNotifications();
  requestNotificationRefresh();
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function normalizedSourceId(sourceId) {
  return sourceId && sourceId !== "all" ? sourceId : null;
}

function segmentCoversTimestamp(seg, sourceId, ts) {
  const srcId = normalizedSourceId(sourceId);
  return Boolean(
    seg &&
    Number.isFinite(ts) &&
    seg.end_ts != null &&
    (!srcId || seg.source_id === srcId) &&
    Number(seg.start_ts) <= ts &&
    Number(seg.end_ts) > ts
  );
}

function segmentCoversEvent(seg, evt) {
  return Boolean(evt && segmentCoversTimestamp(seg, evt.source_id, evt.abs_ts));
}

function recordedSegmentForEvent(evt) {
  if (!evt) return null;
  if (evt.segment_id != null) {
    const byId = st.segments.find(s => String(s.id) === String(evt.segment_id) && segmentCoversEvent(s, evt));
    if (byId) return byId;
  }
  return st.segments.find(s => segmentCoversEvent(s, evt)) || null;
}

async function resolveVideoTimestamp(sourceId, ts, opts = {}) {
  const srcId = normalizedSourceId(sourceId);
  if (!srcId || !Number.isFinite(ts)) return null;
  const p = new URLSearchParams({
    source: srcId,
    ts: String(ts),
    playback: "hls",
  });
  if (Number.isFinite(Number(opts.preRoll))) p.set("pre_roll", String(Math.max(0, Number(opts.preRoll))));
  if (Number.isFinite(Number(opts.window))) p.set("window", String(Math.max(2, Number(opts.window))));
  const r = await fetch(`/api/video/resolve?${p}`, { cache: "no-store" }).catch(() => null);
  if (!r?.ok) return null;
  return await r.json().catch(() => null);
}

function isRecordedResolution(resolved) {
  return Boolean(
    resolved &&
    (resolved.storage_provider === "mp4" || resolved.provider === "mp4") &&
    resolved.url &&
    Number.isFinite(Number(resolved.media_epoch))
  );
}

async function seekLiveTimestamp(sourceId, lookupTs, desiredTs) {
  const chosen = chooseLiveSource(sourceId);
  if (!chosen) return false;
  const win = await fetchLiveWindow(chosen);
  if (!liveWindowContains(win, lookupTs)) return false;
  const liveTs = liveWindowContains(win, desiredTs) ? desiredTs : lookupTs;
  return seekLiveTail(chosen, liveTs);
}

async function seekToTimestamp(sourceId, ts, options = {}) {
  const desiredTs = Number(ts);
  const lookupTs = Number.isFinite(options.lookupTs) ? Number(options.lookupTs) : desiredTs;
  if (!Number.isFinite(desiredTs) || !Number.isFinite(lookupTs)) return false;

  const srcId = normalizedSourceId(sourceId);
  const scrollTs = Number.isFinite(options.scrollTs) ? Number(options.scrollTs) : desiredTs;
  const retries = Number.isFinite(options.retries) ? Math.max(0, Math.floor(options.retries)) : ABSOLUTE_SEEK_RETRIES;
  const retryMs = Number.isFinite(options.retryMs) ? Math.max(0, Number(options.retryMs)) : ABSOLUTE_SEEK_RETRY_MS;
  const updateHistory = options.updateHistory !== false;
  const autoplay = options.autoplay !== false;
  const seq = ++absoluteSeekSeq;

  for (let attempt = 0; attempt <= retries; attempt++) {
    if (seq !== absoluteSeekSeq) return false;

    let resolved = null;
    if (srcId) {
      setStatus("BUFFERING");
      resolved = await resolveVideoTimestamp(srcId, lookupTs, {
        preRoll: options.resolvePreRoll,
        window: options.resolveWindow,
      });
    }
    if (seq !== absoluteSeekSeq) return false;

    if (isRecordedResolution(resolved)) {
      const mediaEpoch = Number(resolved.media_epoch);
      const duration = Number(resolved.duration);
      const maxPosition = Number.isFinite(duration) ? Math.max(0, duration - 0.25) : Infinity;
      const startPosition = Math.max(0, Math.min(maxPosition, desiredTs - mediaEpoch));
      stopLiveTail(false);
      mode.stop();
      el.liveVideo.style.display = "none";
      el.empty.style.display = "none";
      el.video.style.display = "block";
      const landing = await player.seekRecorded({
        url: resolved.url,
        mediaEpoch,
        duration,
        startPosition,
        sourceId: resolved.source_id ?? srcId,
        segmentId: resolved.segment_id,
        requestedTs: desiredTs,
        autoplay,
      });
      if (seq !== absoluteSeekSeq) return false;
      if (landing) {
        setStatus("REPLAY");
        if (autoplay) player.play();
        if (updateHistory) pushState();
        if (options.scroll) scrollTimelineToTs(scrollTs);
        return true;
      }
    }

    const liveOk = await seekLiveTimestamp(srcId, lookupTs, desiredTs);
    if (seq !== absoluteSeekSeq) return false;
    if (liveOk) {
      if (options.scroll) scrollTimelineToTs(scrollTs);
      return true;
    }

    if (attempt < retries) await sleep(retryMs);
  }

  setStatus("NONE");
  return false;
}

async function seekToEvent(evt, { scroll = true } = {}) {
  if (!evt) return false;
  const desiredTs = Number(evt.display_ts ?? evt.abs_ts);
  return seekToTimestamp(evt.source_id, desiredTs, {
    lookupTs: desiredTs,
    scroll,
    scrollTs: desiredTs,
  });
}

function isEventActive(evt, ts) {
  if (ts == null) return false;
  const eventTs = Number(evt.display_ts ?? evt.abs_ts);
  if (!Number.isFinite(eventTs)) return false;
  if (evt.provisional) {
    const recorded = recordedSegmentForEvent(evt);
    if (recorded && player.currentSeg?.id === recorded.id) {
      return ts >= eventTs && ts <= eventTs + Math.max(1, evt.end_off ?? 1);
    }
    return liveTail.active && liveTail.srcId === evt.source_id && Math.abs(ts - eventTs) <= 3;
  }
  if (player.currentSeg?.id !== evt.segment_id) return false;
  const dur = Math.max(1, (evt.end_off ?? 0) - (evt.start_off ?? 0));
  return ts >= eventTs && ts <= eventTs + dur;
}

function renderNearestEvents() {
  if (!el.eventThumbs) return;
  renderNearScope();
  const baseTs = player.displayTs ?? st.events[0]?.abs_ts ?? Date.now() / 1000;
  const evts = nearestEvents(baseTs);
  const scope = nearbyScopeLabel();
  const sig = `${st.source}|${scope}|${evts.map(e => e.id).join(",") || "empty"}`;

  if (!evts.length) {
    if (_nearListSig === sig) return;
    _nearListSig = sig;
    el.eventThumbs.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "v2-event-thumbs-empty";
    empty.textContent = scope === "all" ? "No events" : `No ${scope} events`;
    el.eventThumbs.appendChild(empty);
    return;
  }

  if (_nearListSig === sig) {
    _updateEventRows(evts, baseTs);
    return;
  }

  // Don't destroy DOM while user is hovering — would lose hover state and cause flicker
  if (_nearHover) {
    _updateEventRows(evts, baseTs);
    return;
  }

  _nearListSig = sig;
  _renderEventBuckets(evts, baseTs);
}

function classLabel(cls) {
  if (!cls) return "Motion";
  return cls.slice(0, 1).toUpperCase() + cls.slice(1);
}

function eventTag(cls) {
  const clean = String(cls || "motion").trim();
  return clean.slice(0, 2).toUpperCase();
}

function eventDurationLabel(evt) {
  const seconds = Math.max(1, Math.round((evt.end_off ?? 0) - (evt.start_off ?? 0)));
  if (seconds < 60) return `+${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s ? `+${m}m ${s}s` : `+${m}m`;
}

function bucketStart(ts) {
  return Math.floor(ts / (15 * 60)) * 15 * 60;
}

function bucketLabel(start) {
  const end = start + 15 * 60;
  return `${eventLocalTime(start).slice(0, 5)} - ${eventLocalTime(end).slice(0, 5)}`;
}

function _makeThumbNode(evt, baseTs) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "v2-event-row v2-event-thumb"
    + (evt.provisional ? " provisional" : "")
    + (isEventActive(evt, baseTs) ? " active" : "");
  btn.dataset.eventId = String(evt.id);
  btn.title = `${evt.class} ${eventLocalTime(evt.abs_ts)} ${sourceLabel(evt.source_id)}`;
  btn.addEventListener("click", async () => {
    await seekToEvent(evt);
    closeActivityAfterMobilePick();
  });

  const thumb = document.createElement("div");
  thumb.className = "ev-thumb";
  let media;
  media = document.createElement("img");
  media.loading = "lazy";
  media.alt = "";
  media.src = `/api/video/event-thumb/${encodeURIComponent(String(evt.id))}`;
  media.onerror = () => {
    if (!evt.provisional) return;
    const fallback = document.createElement("div");
    fallback.className = "ev-thumb-live";
    fallback.textContent = "LIVE";
    media.replaceWith(fallback);
    media = fallback;
  };
  const tag = document.createElement("div");
  tag.className = "ev-thumb-tag";
  tag.textContent = eventTag(evt.class);
  thumb.append(media, tag);

  const meta = document.createElement("div");
  meta.className = "ev-meta";
  const klass = document.createElement("div");
  klass.className = "ev-cls";
  klass.textContent = classLabel(evt.class);
  const time = document.createElement("div");
  time.className = "ev-time";
  time.textContent = `${eventLocalTime(evt.abs_ts)} · `;
  const d = document.createElement("span");
  d.className = "dur";
  d.dataset.nearDist = "1";
  d.textContent = eventDurationLabel(evt);
  time.appendChild(d);
  meta.append(klass, time);

  btn.append(thumb, meta);
  return btn;
}

function _updateThumbNode(btn, evt, baseTs) {
  btn.classList.toggle("active", isEventActive(evt, baseTs));
  const dist = btn.querySelector("[data-near-dist]");
  const label = eventDurationLabel(evt);
  if (dist && dist.textContent !== label) dist.textContent = label;
}

function _updateEventRows(evts, baseTs) {
  const nodes = el.eventThumbs.querySelectorAll(".v2-event-row");
  evts.forEach((evt, i) => {
    const btn = nodes[i];
    if (!btn || btn.dataset.eventId !== String(evt.id)) return;
    _updateThumbNode(btn, evt, baseTs);
  });
}

function _renderEventBuckets(evts, baseTs) {
  el.eventThumbs.innerHTML = "";
  const buckets = new Map();
  evts.forEach(evt => {
    const key = bucketStart(evt.abs_ts);
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(evt);
  });
  [...buckets.entries()]
    .sort((a, b) => b[0] - a[0])
    .forEach(([start, bucketEvents]) => {
      const group = document.createElement("div");
      group.className = "ev-day";
      const h = document.createElement("div");
      h.className = "ev-day-h";
      const count = bucketEvents.length;
      h.innerHTML = `<span></span><span></span>`;
      h.children[0].textContent = bucketLabel(start);
      h.children[1].textContent = `${count} ${count === 1 ? "event" : "events"}`;
      group.appendChild(h);
      bucketEvents
        .sort((a, b) => b.abs_ts - a.abs_ts)
        .forEach(evt => group.appendChild(_makeThumbNode(evt, baseTs)));
      el.eventThumbs.appendChild(group);
    });
}

let _nearRenderPending = false;
let _lastNearRender = 0;
let _nearListSig = "";
let _nearHover = false;
el.eventThumbs?.addEventListener("mouseenter", () => { _nearHover = true; });
el.eventThumbs?.addEventListener("mouseleave", () => { _nearHover = false; });
function scheduleNearestEvents(force = false) {
  const now = performance.now();
  if (!force && now - _lastNearRender < NEAR_EVENT_REFRESH_MS) return;
  _lastNearRender = now;
  if (_nearRenderPending) return;
  _nearRenderPending = true;
  requestAnimationFrame(() => {
    _nearRenderPending = false;
    renderNearestEvents();
  });
}

// ── Data loading ──────────────────────────────────────
const _loadBar = document.getElementById("v2LoadBar");
let _loadCount = 0;
function _loadStart() { _loadCount++; _loadBar?.classList.add("loading"); }
function _loadEnd()   { if (--_loadCount <= 0) { _loadCount = 0; _loadBar?.classList.remove("loading"); } }

async function load() {
  _loadStart();
  const p = new URLSearchParams();
  if (st.source !== "all") p.set("source", st.source);
  appendZoneParam(p);

  // Events are loaded for a buffered range (±12h) around the visible window.
  // Only re-fetch if the visible window has moved outside the already-loaded range.
  const evFrom = st.window.from - EVENTS_BUFFER;
  const evTo   = st.window.to   + EVENTS_BUFFER;
  const segFrom = st.window.from - SEGMENTS_BUFFER;
  const segTo   = st.window.to   + SEGMENTS_BUFFER;
  const segParams = new URLSearchParams(p);
  segParams.set("since", String(Math.floor(segFrom)));
  segParams.set("until", String(Math.ceil(segTo)));
  const _loadCheckTo = Math.min(st.window.to, Date.now() / 1000);
  const needsEventsLoad = _loadCheckTo > st.window.from && !_eventsRangesCovers(st.window.from, _loadCheckTo);
  if (needsEventsLoad) timeline.setFetchingRange(evFrom, evTo);

  const [sr, evR, cr, zr] = await Promise.all([
    fetch(`/api/video2/timeline?${segParams}`, { cache:"no-store" }).then(r=>r.json()).catch(()=>({})),
    needsEventsLoad
      ? fetch(`/api/video/events?since=${Math.floor(evFrom)}&until=${Math.ceil(evTo)}&${p}`, { cache:"no-store" }).then(r=>r.json()).catch(()=>({}))
      : Promise.resolve(null),
    fetch(`/api/video/classes?${p}`, { cache:"no-store" }).then(r=>r.json()).catch(()=>({})),
    fetchZonesForSource(),
  ]);

  st.segments = (sr.segments || []).map(worldizeSeg);
  if (sr.bounds) st.segmentBounds = sr.bounds;
  st.classes  = cr.classes  || {};
  if (zr) {
    st.zones = zr.zones || [];
    st.zonesSource = st.source;
  }

  if (evR) {
    // Merge new events into st.events (accumulate, don't replace)
    const byId = new Map(st.events.map(e => [e.id, e]));
    (evR.events || []).forEach(e => byId.set(e.id, e));
    st.events = [...byId.values()].sort((a, b) => b.abs_ts - a.abs_ts);
    // Coverage tracking: cap at now (events can't exist in the future).
    // This stops the live-window perpetual reload loop.
    _eventsRangesAdd(evFrom, Math.min(evTo, Date.now() / 1000));
  }
  timeline.clearFetchingRange();
  // Green bar: show only up to last YOLO-tagged segment, not the live gap.
  const latestSegEnd = st.segments.reduce((m, s) => s.end_ts ? Math.max(m, s.end_ts) : m, 0);
  const visRanges = latestSegEnd > 0
    ? st.eventsLoaded.ranges.map(r => ({ from: r.from, to: Math.min(r.to, latestSegEnd) })).filter(r => r.to > r.from)
    : st.eventsLoaded.ranges;
  timeline.setEventsRanges(visRanges);

  // Advance right-edge only when viewing recent content (within 2h of now)
  // Scrolling into history must not reset window.to to now — that causes
  // the events since/until to span days and hit the 10k limit, losing old events
  const nowTs = Date.now() / 1000;
  if (nowTs > st.window.to - 60 && st.window.to > nowTs - 7200) {
    st.window.to = nowTs + LIVE_TIMELINE_FUTURE_PAD_SECONDS;
    setTimelineWindow(st.window.from, st.window.to);
  }

  player.setSegments(st.segments);

  const srcNames = {};
  st.sources.forEach(s => srcNames[s.id] = s.name || s.id);
  timeline.setSrcNames(srcNames);
  timeline.setData(allSegsForSrc(), filteredEvts());

  await fetchSourceStatus();
  renderSrcCtrl();
  await fetchActivitySummary();
  updateActivityCount();
  renderClsCtrl();
  updateZoneControl();
  drawZones();
  renderNearScope();
  scheduleNearestEvents(true);

  if (!liveTail.active && !st.initDone && st.segments.length) {
    st.initDone = true;
    const latest = st.segments.find(s => s.end_ts != null);
    if (latest) {
      el.empty.style.display = "none";
      el.video.style.display = "block";
      seekToTimestamp(latest.source_id, Math.max(latest.start_ts, latest.end_ts - 1), {
        autoplay: true,
        retries: 0,
        updateHistory: false,
      });
    }
  }
  _loadEnd();
  _scheduleGapFill();
}

// ── Background gap filler ─────────────────────────────
// After each load, find gaps in loaded ranges and fill them one at a time.
// Fills only gaps within the last 24h — no point fetching ancient history.
let _gapFillTimer = null;

function _scheduleGapFill() {
  clearTimeout(_gapFillTimer);
  _gapFillTimer = setTimeout(_fillNextGap, 100);
}

async function _fillNextGap() {
  const ranges = st.eventsLoaded.ranges;
  if (ranges.length < 2) return;

  const nowTs = Date.now() / 1000;
  const p = new URLSearchParams();
  if (st.source !== "all") p.set("source", st.source);
  appendZoneParam(p);

  for (let i = 0; i < ranges.length - 1; i++) {
    const gapFrom = ranges[i].to;
    const gapTo   = Math.min(ranges[i + 1].from, nowTs);
    if (gapTo - gapFrom < 30) continue;

    const evR = await fetch(
      `/api/video/events?since=${Math.floor(gapFrom)}&until=${Math.ceil(gapTo)}&${p}`,
      { cache: "no-store" }
    ).then(r => r.json()).catch(() => ({}));

    const byId = new Map(st.events.map(e => [e.id, e]));
    (evR.events || []).forEach(e => byId.set(e.id, e));
    st.events = [...byId.values()].sort((a, b) => b.abs_ts - a.abs_ts);
    _eventsRangesAdd(gapFrom, Math.min(gapTo, nowTs));

    const latestSegEnd = st.segments.reduce((m, s) => s.end_ts ? Math.max(m, s.end_ts) : m, 0);
    const vis = latestSegEnd > 0
      ? st.eventsLoaded.ranges.map(r => ({ from: r.from, to: Math.min(r.to, latestSegEnd) })).filter(r => r.to > r.from)
      : st.eventsLoaded.ranges;
    timeline.setEventsRanges(vis);
    timeline.setData(allSegsForSrc(), filteredEvts());
    scheduleNearestEvents(true);

    if (st.eventsLoaded.ranges.length >= 2) _scheduleGapFill();
    return;
  }
}

// ── Source control ────────────────────────────────────
function renderSrcCtrl() {
  if (!el.srcCtrl) return;
  el.srcCtrl.innerHTML = "";
  const rtsp = st.sources.filter(s => s.type === "rtsp");
  if (el.emptyText) el.emptyText.textContent = rtsp.length ? "Choose a source to start" : "No cameras added yet";
  if (el.emptyCta)  el.emptyCta.hidden = !!rtsp.length;
  if (!rtsp.length) return;

  const items = rtsp.length > 1 ? [{ id:"all", name:"All" }, ...rtsp] : rtsp;
  items.forEach(s => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ab-src-pill" + (st.source === s.id ? " active" : "");
    const label = document.createElement("span");
    label.textContent = s.name || s.id;
    b.appendChild(label);
    if (s.id !== "all") {
      const dot = document.createElement("span");
      dot.className = "dot";
      const state = sourceState(s.id);
      dot.classList.toggle("offline",   state === "offline");
      dot.classList.toggle("buffering", state === "buffering");
      b.appendChild(dot);
    }
    b.addEventListener("click", () => {
      if (st.source === s.id) return;
      const wasLive = liveTail.active;
      cancelZoneEditor();
      cancelClipSelection();
      stopLiveTail(false);
      st.source = s.id; st.initDone = false;
      st.events = []; st.segmentBounds = null; st.zones = []; st.zonesSource = null; st.activeZoneId = null; _eventsRangesClear();
      renderSrcCtrl(); load().then(pushState);
      if (wasLive) startLiveTail(s.id);
    });
    el.srcCtrl.appendChild(b);
  });
}

// ── Class filter ──────────────────────────────────────
function renderClsCtrl() {
  el.clsCtrl.innerHTML = "";
  const counts = { ...(st.classes || {}), ...(st.summary.classes || {}) };
  const entries = Object.entries(counts)
    .filter(([cls, n]) => cls && n > 0)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  if (!entries.length) {
    el.clsField.hidden = true;
    return;
  }
  el.clsField.hidden = false;

  const total = st.summary.total || Object.values(counts).reduce((a, b) => a + b, 0);
  const allBtn = document.createElement("button");
  allBtn.type = "button";
  allBtn.className = "class-chip" + (st.cls.size === 0 && st.xls.size === 0 ? " active" : "");
  allBtn.innerHTML = `<span>All</span><span class="count"></span>`;
  allBtn.querySelector(".count").textContent = String(total);
  allBtn.addEventListener("click", () => {
    st.cls.clear(); st.xls.clear();
    pushState();
    handleClassSelectionChanged(new Set());
  });
  el.clsCtrl.appendChild(allBtn);

  entries.forEach(([cls, n]) => {
    const b = document.createElement("button");
    b.type = "button";
    const included = st.cls.has(cls);
    const excluded = st.xls.has(cls);
    b.className = "class-chip" + (included ? " active" : excluded ? " excluded" : "");
    b.innerHTML = `<span></span><span class="count"></span>`;
    b.children[0].textContent = (excluded ? "✕ " : "") + classLabel(cls);
    b.children[1].textContent = String(n);
    b.addEventListener("click", () => {
      if (st.cls.has(cls)) {
        st.cls.delete(cls); st.xls.add(cls);      // included → excluded
      } else if (st.xls.has(cls)) {
        st.xls.delete(cls);                        // excluded → neutral
      } else {
        st.cls.add(cls); st.xls.delete(cls);      // neutral → included
      }
      pushState();
      // Synchronous visual update first — before the async playlist search
      renderClsCtrl();
      timeline.setData(allSegsForSrc(), filteredEvts());
      scheduleNearestEvents(true);
      handleClassSelectionChanged(new Set(st.cls));
    });
    el.clsCtrl.appendChild(b);
  });
}

// ── Clip range selection ──────────────────────────────
function currentClipAnchor() {
  const ts = liveTail.active ? liveTailCurrentTs() : player.reliableTs;
  const sourceId = liveTail.active
    ? liveTail.srcId
    : (player.currentSeg?.source_id ?? (st.source !== "all" ? st.source : null));
  return ts && sourceId ? { ts, sourceId } : null;
}

function sourceClosedBounds(sourceId) {
  if (st.segmentBounds?.from != null && st.segmentBounds?.to != null) {
    return { from: Number(st.segmentBounds.from), to: Number(st.segmentBounds.to) };
  }
  const segs = st.segments
    .filter(s => s.source_id === sourceId && s.end_ts != null)
    .sort((a, b) => a.start_ts - b.start_ts);
  if (!segs.length) return null;
  return { from: segs[0].start_ts, to: Math.max(...segs.map(s => s.end_ts)) };
}

function normalizeClipRange(start, end, sourceId, fixed = null) {
  let a = Math.min(start, end);
  let b = Math.max(start, end);
  const bounds = sourceClosedBounds(sourceId);
  if (bounds) {
    a = Math.max(bounds.from, Math.min(bounds.to, a));
    b = Math.max(bounds.from, Math.min(bounds.to, b));
  }
  if (b - a < CLIP_MIN_DURATION) {
    if (fixed === "start") b = a + CLIP_MIN_DURATION;
    else if (fixed === "end") a = b - CLIP_MIN_DURATION;
    else {
      const mid = (a + b) / 2;
      a = mid - CLIP_MIN_DURATION / 2;
      b = mid + CLIP_MIN_DURATION / 2;
    }
  }
  if (b - a > CLIP_MAX_DURATION) {
    if (fixed === "start") b = a + CLIP_MAX_DURATION;
    else if (fixed === "end") a = b - CLIP_MAX_DURATION;
    else {
      const mid = (a + b) / 2;
      a = mid - CLIP_MAX_DURATION / 2;
      b = mid + CLIP_MAX_DURATION / 2;
    }
  }
  if (bounds) {
    const dur = b - a;
    if (a < bounds.from) { a = bounds.from; b = Math.min(bounds.to, a + dur); }
    if (b > bounds.to) { b = bounds.to; a = Math.max(bounds.from, b - dur); }
  }
  return { start: a, end: b };
}

function startClipSelection({ showToolbar = false } = {}) {
  const anchor = currentClipAnchor();
  if (!anchor) {
    setStatus("NONE");
    return;
  }
  const range = normalizeClipRange(
    anchor.ts - CLIP_DEFAULT_BEFORE,
    anchor.ts + CLIP_DEFAULT_AFTER,
    anchor.sourceId,
  );
  st.clip.active = true;
  st.clip.toolbarOpen = showToolbar;
  st.clip.start = range.start;
  st.clip.end = range.end;
  st.clip.sourceId = anchor.sourceId;
  st.clip.drag = null;
  syncClipSelection();
}

function setClipPreviewing(previewing) {
  st.clip.previewing = !!previewing;
  if (!st.clip.previewing) {
    st.clip.previewSeq++;
    st.clip.previewRestarting = false;
  }
  if (!el.clipPreview) return;
  if (!el.clipPreview.dataset.idleText) {
    el.clipPreview.dataset.idleText = el.clipPreview.textContent || "Preview";
  }
  el.clipPreview.classList.toggle("previewing", st.clip.previewing);
  el.clipPreview.textContent = st.clip.previewing ? "Previewing" : el.clipPreview.dataset.idleText;
}

function stopClipPreview({ pause = false } = {}) {
  if (!st.clip.previewing) return;
  setClipPreviewing(false);
  if (mode.current === "playlist") mode.stop();
  if (pause) player.pause();
}

function cancelClipSelection() {
  stopClipPreview({ pause: true });
  st.clip.active = false;
  st.clip.toolbarOpen = false;
  st.clip.start = null;
  st.clip.end = null;
  st.clip.sourceId = null;
  st.clip.drag = null;
  syncClipSelection();
}

function setClipRange(start, end, fixed = null) {
  if (!st.clip.active || !st.clip.sourceId) return;
  stopClipPreview({ pause: true });
  const range = normalizeClipRange(start, end, st.clip.sourceId, fixed);
  st.clip.start = range.start;
  st.clip.end = range.end;
  syncClipSelection();
}

function syncClipSelection() {
  const active = st.clip.active && st.clip.start != null && st.clip.end != null && st.clip.sourceId;
  timeline.setClipRange(active ? {
    active: true,
    start: st.clip.start,
    end: st.clip.end,
    source_id: st.clip.sourceId,
  } : null);
  el.download?.classList.toggle("clip-active", !!active);
  const showToolbar = active && st.clip.toolbarOpen;
  if (el.clipToolbar) el.clipToolbar.hidden = !showToolbar;
  if (el.clipPreview) {
    el.clipPreview.disabled = !active;
    el.clipPreview.classList.toggle("previewing", !!st.clip.previewing);
  }
  if (!active) return;

  const mid = (st.clip.start + st.clip.end) / 2;
  if (showToolbar && el.clipRange) {
    el.clipRange.textContent = `${formatClock(st.clip.start)} - ${formatClock(st.clip.end)} · ${formatClipDuration(st.clip.end - st.clip.start)}`;
  }
  if (showToolbar && el.clipToolbar && el.tlCanvas) {
    const canvasW = el.tlCanvas.clientWidth || 1;
    const toolbarW = el.clipToolbar.offsetWidth || 240;
    const x = Math.max(toolbarW / 2, Math.min(canvasW - toolbarW / 2, timeline.tsToX(mid)));
    el.clipToolbar.style.left = `${x}px`;
  }
}

function previewSelectedClip() {
  if (!st.clip.active || !st.clip.sourceId || st.clip.start == null || st.clip.end == null) {
    startClipSelection({ showToolbar: true });
    return;
  }
  const start = Math.min(st.clip.start, st.clip.end);
  const end = Math.max(st.clip.start, st.clip.end);
  if (end <= start) return;
  stopLiveTail(false);
  mode.stop();
  const seq = ++st.clip.previewSeq;
  st.clip.previewRestarting = false;
  setClipPreviewing(true);
  centerWindowOn(start);
  scrollTimelineToTs(start);
  setStatus("PREVIEW");
  seekResolvedClipPreview(start, end, st.clip.sourceId, seq);
}

async function seekResolvedClipPreview(start, end, sourceId, seq = st.clip.previewSeq) {
  if (!st.clip.previewing || seq !== st.clip.previewSeq) return false;
  const duration = Math.max(2, end - start);
  const ok = await seekToTimestamp(sourceId, start, {
    autoplay: true,
    retries: 0,
    updateHistory: false,
    resolvePreRoll: 0,
    resolveWindow: duration,
  });
  if (!st.clip.previewing || seq !== st.clip.previewSeq) return false;
  st.clip.previewRestarting = false;
  setStatus(ok ? "PREVIEW" : "NONE");
  return ok;
}

function maybeLoopResolvedClipPreview(ts) {
  if (!st.clip.previewing || st.clip.previewRestarting) return;
  if (!st.clip.sourceId || st.clip.start == null || st.clip.end == null || ts == null) return;
  const start = Math.min(st.clip.start, st.clip.end);
  const end = Math.max(st.clip.start, st.clip.end);
  if (ts < end - 0.15) return;
  st.clip.previewRestarting = true;
  seekResolvedClipPreview(start, end, st.clip.sourceId, st.clip.previewSeq);
}

function clipDownloadToken() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function clipDownloadStarted(token) {
  return document.cookie
    .split(";")
    .map(c => c.trim())
    .some(c => c === `wanyard_clip_download=${encodeURIComponent(token)}`);
}

function clearClipDownloadCookie() {
  document.cookie = "wanyard_clip_download=; Max-Age=0; path=/; SameSite=Lax";
}

function setClipDownloadBusy(busy) {
  st.clip.downloading = busy;
  if (st.clip.downloadTimer) {
    clearTimeout(st.clip.downloadTimer);
    st.clip.downloadTimer = null;
  }
  el.download?.classList.toggle("loading", busy);
  if (!el.clipDownload) return;
  if (!el.clipDownload.dataset.idleText) {
    el.clipDownload.dataset.idleText = el.clipDownload.textContent || "Download";
  }
  el.clipDownload.classList.toggle("loading", busy);
  el.clipDownload.disabled = busy;
  el.clipDownload.textContent = busy ? "Preparing..." : el.clipDownload.dataset.idleText;
}

function waitForClipDownloadStart(token) {
  const startedAt = Date.now();
  const poll = () => {
    if (clipDownloadStarted(token)) {
      clearClipDownloadCookie();
      setClipDownloadBusy(false);
      return;
    }
    if (Date.now() - startedAt > 90000) {
      setClipDownloadBusy(false);
      setStatus("NONE");
      return;
    }
    st.clip.downloadTimer = setTimeout(poll, 400);
  };
  st.clip.downloadTimer = setTimeout(poll, 400);
}

function downloadSelectedClip() {
  if (st.clip.downloading) return;
  if (!st.clip.active || !st.clip.sourceId || st.clip.start == null || st.clip.end == null) {
    startClipSelection({ showToolbar: true });
    return;
  }
  const start = Math.min(st.clip.start, st.clip.end);
  const end = Math.max(st.clip.start, st.clip.end);
  const ts = (start + end) / 2;
  const p = new URLSearchParams({
    source: st.clip.sourceId,
    ts: ts.toFixed(3),
    before: (ts - start).toFixed(3),
    after: (end - ts).toFixed(3),
  });
  if (st.showBoxes) p.set("boxes", "1");
  if (st.cls.size > 0) p.set("classes", [...st.cls].join(","));
  if (st.xls.size > 0) p.set("exclude_classes", [...st.xls].join(","));
  const token = clipDownloadToken();
  p.set("download_token", token);
  clearClipDownloadCookie();
  setClipDownloadBusy(true);
  const a = document.createElement("a");
  a.href = `/api/video/clip?${p}`;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
  waitForClipDownloadStart(token);
}

// ── Timeline drag-to-scroll ───────────────────────────
let _drag = null, _wasDrag = false;
let _clipSuppressClick = false;
let _timelinePointerId = null;

function timelineLocalPoint(e) {
  const rect = el.tlCanvas.getBoundingClientRect();
  return { x: e.clientX - rect.left, y: e.clientY - rect.top, rect };
}

function captureTimelinePointer(e) {
  if (e.pointerId == null) return;
  try { el.tlCanvas.setPointerCapture?.(e.pointerId); } catch {}
}

function releaseTimelinePointer(e) {
  if (e?.pointerId == null) return;
  try { el.tlCanvas.releasePointerCapture?.(e.pointerId); } catch {}
}

function beginTimelineDrag(e) {
  if (e.button != null && e.button !== 0) return;
  const pt = timelineLocalPoint(e);
  if (st.clip.active) {
    const hit = timeline.clipHit(pt.x, pt.y);
    if (hit) {
      st.clip.drag = {
        part: hit.part,
        x: pt.x,
        start: st.clip.start,
        end: st.clip.end,
        ts: hit.ts ?? timeline.xToTs(pt.x),
      };
      el.tlCanvas.style.cursor = hit.part === "move" ? "grabbing" : "ew-resize";
      _clipSuppressClick = true;
      _timelinePointerId = e.pointerId ?? null;
      captureTimelinePointer(e);
      e.preventDefault();
      return;
    }
  }
  _drag = { startX: e.clientX, fromSnap: st.window.from, toSnap: st.window.to, moved: false };
  el.tlCanvas.style.cursor = "grabbing";
  _timelinePointerId = e.pointerId ?? null;
  captureTimelinePointer(e);
  e.preventDefault();
}

function moveTimelineDrag(e) {
  if (_timelinePointerId != null && e.pointerId != null && e.pointerId !== _timelinePointerId) return;
  if (st.clip.drag) {
    const pt = timelineLocalPoint(e);
    const ts = timeline.xToTs(pt.x);
    const part = st.clip.drag.part;
    if (part === "start") {
      setClipRange(ts, st.clip.drag.end, "end");
    } else if (part === "end") {
      setClipRange(st.clip.drag.start, ts, "start");
    } else {
      const delta = ts - st.clip.drag.ts;
      setClipRange(st.clip.drag.start + delta, st.clip.drag.end + delta);
    }
    e.preventDefault();
    return;
  }
  if (!_drag) return;
  const dx = e.clientX - _drag.startX;
  if (Math.abs(dx) > 4) _drag.moved = true;
  if (!_drag.moved) return;
  const rect = el.tlCanvas.getBoundingClientRect();
  const span = _drag.toSnap - _drag.fromSnap;
  const pxPerSec = Math.max(1, rect.width - timeline.labelWidth) / span;
  const shift = -dx / pxPerSec;
  const { oldest, newest } = _windowBounds();
  let nf = _drag.fromSnap + shift, nt = _drag.toSnap + shift;
  if (nf < oldest) { nf = oldest; nt = oldest + span; }
  if (nt > newest) { nt = newest; nf = newest - span; }
  if (nf < oldest) nf = oldest;
  st.window.from = nf; st.window.to = nt;
  setTimelineWindow(nf, nt);
  e.preventDefault();
}

function endTimelineDrag(e) {
  if (_timelinePointerId != null && e.pointerId != null && e.pointerId !== _timelinePointerId) return;
  releaseTimelinePointer(e);
  _timelinePointerId = null;
  if (st.clip.drag) {
    st.clip.drag = null;
    el.tlCanvas.style.cursor = "";
    return;
  }
  if (!_drag) return;
  el.tlCanvas.style.cursor = "";
  _wasDrag = _drag.moved;
  if (_drag.moved) {
    clearTimeout(_fetchDebounce);
    _fetchDebounce = setTimeout(() => load(), 400);
  }
  _drag = null;
}

if (window.PointerEvent) {
  el.tlCanvas.addEventListener("pointerdown", beginTimelineDrag);
  el.tlCanvas.addEventListener("pointermove", moveTimelineDrag);
  el.tlCanvas.addEventListener("pointerup", endTimelineDrag);
  el.tlCanvas.addEventListener("pointercancel", endTimelineDrag);
} else {
  el.tlCanvas.addEventListener("mousedown", beginTimelineDrag);
  window.addEventListener("mousemove", moveTimelineDrag);
  window.addEventListener("mouseup", endTimelineDrag);
}

// ── Timeline interactions ─────────────────────────────
let _clickTimer = null;
el.tlCanvas.addEventListener("click", e => {
  if (_clipSuppressClick) { _clipSuppressClick = false; return; }
  if (_wasDrag) { _wasDrag = false; return; }
  const rect = el.tlCanvas.getBoundingClientRect();
  const hit  = timeline.decode(e.clientX - rect.left, e.clientY - rect.top);
  if (!hit) return;
  // Delay single-click action so dblclick can cancel it
  clearTimeout(_clickTimer);
  _clickTimer = setTimeout(async () => {
    if (hit.snapEvent) {
      await seekToEvent(hit.snapEvent, { scroll: false });
      return;
    } else {
      await seekToTimestamp(hit.srcId, Math.min(hit.ts, Date.now() / 1000));
    }
  }, 220);
});

// Timeline scroll + zoom
const TL_MIN_SPAN = 30;          // 30s — sub-segment detail
const TL_MAX_SPAN = 7 * 86400;   // 7 days

let _fetchDebounce = null;
let _scrollVel = 0;
let _scrollRaf = null;
let _zoomRaf   = null;

function _windowBounds() {
  const archiveFrom = Number(st.segmentBounds?.from);
  const archiveTo = Number(st.segmentBounds?.to);
  return {
    oldest: Number.isFinite(archiveFrom) ? archiveFrom - 1800 : st.window.from,
    newest: Math.max(
      Date.now() / 1000 + LIVE_TIMELINE_FUTURE_PAD_SECONDS,
      Number.isFinite(archiveTo) ? archiveTo : 0,
    ),
  };
}

function _applyWindowShift(shift) {
  const span = st.window.to - st.window.from;
  const { oldest, newest } = _windowBounds();
  let newFrom = st.window.from + shift;
  let newTo   = st.window.to   + shift;
  if (newFrom < oldest) { newFrom = oldest; newTo = oldest + span; }
  if (newTo   > newest) { newTo = newest;   newFrom = newest - span; }
  if (newFrom < oldest)   newFrom = oldest;
  st.window.from = newFrom; st.window.to = newTo;
  setTimelineWindow(newFrom, newTo);
}

function _applyZoom(factor, cursorX, rectWidth) {
  const span   = st.window.to - st.window.from;
  const plotW  = Math.max(1, rectWidth - timeline.labelWidth);
  const newSpan = Math.max(TL_MIN_SPAN, Math.min(TL_MAX_SPAN, span * factor));
  if (newSpan === span) return;
  const pivotFrac = Math.max(0, Math.min(1, (cursorX - timeline.labelWidth) / plotW));
  const pivotTs   = st.window.from + pivotFrac * span;
  const { oldest, newest } = _windowBounds();
  let nf = pivotTs - pivotFrac * newSpan;
  let nt = nf + newSpan;
  if (nf < oldest) { nf = oldest; nt = oldest + newSpan; }
  if (nt > newest) { nt = newest; nf = newest - newSpan; }
  if (nf < oldest)   nf = oldest;
  st.window.from = nf; st.window.to = nt;
  setTimelineWindow(nf, nt);
}

function _animateZoomTo(targetFrom, targetTo, ms = 180) {
  if (_zoomRaf) { cancelAnimationFrame(_zoomRaf); _zoomRaf = null; }
  const sf = st.window.from, st0 = st.window.to, t0 = performance.now();
  function step(now) {
    const t = Math.min(1, (now - t0) / ms);
    const e = 1 - Math.pow(1 - t, 3);  // ease-out cubic
    st.window.from = sf + (targetFrom - sf) * e;
    st.window.to   = st0 + (targetTo   - st0) * e;
    setTimelineWindow(st.window.from, st.window.to);
    if (t < 1) _zoomRaf = requestAnimationFrame(step);
    else { _zoomRaf = null; load(); }
  }
  _zoomRaf = requestAnimationFrame(step);
}

function _scrollDecay() {
  if (Math.abs(_scrollVel) < 0.5) { _scrollVel = 0; _scrollRaf = null; return; }
  _applyWindowShift(_scrollVel);
  _scrollVel *= 0.82;
  _scrollRaf = requestAnimationFrame(_scrollDecay);
}

// Ctrl held → show zoom cursor
window.addEventListener("keydown", e => { if (e.key === "Control") el.tlCanvas.style.cursor = "zoom-in"; });
window.addEventListener("keyup",   e => { if (e.key === "Control") el.tlCanvas.style.cursor = ""; });

el.tlCanvas.addEventListener("wheel", e => {
  e.preventDefault();
  const rect = el.tlCanvas.getBoundingClientRect();

  if (e.ctrlKey) {
    // Pinch-to-zoom (trackpad) or Ctrl+scroll (mouse)
    const raw = e.deltaMode === 1 ? e.deltaY * 40 : e.deltaY;
    _applyZoom(Math.exp(raw * 0.006), e.clientX - rect.left, rect.width);
    clearTimeout(_fetchDebounce);
    _fetchDebounce = setTimeout(() => load(), 400);
    return;
  }

  // Scroll / momentum
  const span     = st.window.to - st.window.from;
  const pxPerSec = Math.max(1, rect.width - timeline.labelWidth) / span;
  const rawDelta = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
  const delta    = e.deltaMode === 1 ? rawDelta * 40 : rawDelta;
  const shift    = delta / pxPerSec;
  _scrollVel += shift * 0.35;
  _applyWindowShift(shift * 0.65);
  if (!_scrollRaf) _scrollRaf = requestAnimationFrame(_scrollDecay);
  clearTimeout(_fetchDebounce);
  _fetchDebounce = setTimeout(() => load(), 500);
}, { passive: false });

// Double-click: zoom to 1h centred on click (toggle 1h ↔ 6h)
el.tlCanvas.addEventListener("dblclick", e => {
  clearTimeout(_clickTimer);  // cancel the pending single-click seek
  const rect = el.tlCanvas.getBoundingClientRect();
  const hit  = timeline.decode(e.clientX - rect.left, e.clientY - rect.top);
  if (!hit) return;
  const span = st.window.to - st.window.from;
  const targetSpan = span <= 3600 ? 6 * 3600 : 3600;
  _animateZoomTo(hit.ts - targetSpan * 0.4, hit.ts + targetSpan * 0.6);
});

// Hover → thumbnail preview
let hoverTimer = null;
el.tlCanvas.addEventListener("mousemove", e => {
  const rect = el.tlCanvas.getBoundingClientRect();
  if (st.clip.active && !st.clip.drag && !_drag) {
    const clipHit = timeline.clipHit(e.clientX - rect.left, e.clientY - rect.top);
    if (clipHit) el.tlCanvas.style.cursor = clipHit.part === "move" ? "grab" : "ew-resize";
    else el.tlCanvas.style.cursor = "";
  }
  const hit  = timeline.decode(e.clientX - rect.left, e.clientY - rect.top);
  clearTimeout(hoverTimer);
  if (!hit) { el.thumb.hidden = true; return; }
  hoverTimer = setTimeout(() => {
    // Only closed segments are playable
    const seg = filteredSegs().find(s => s.source_id === hit.srcId &&
      s.end_ts != null && s.start_ts <= hit.ts && s.end_ts > hit.ts);
    if (!seg) { el.thumb.hidden = true; return; }
    const off = Math.max(0, hit.ts - seg.start_ts);
    const img = el.thumb.querySelector("img");
    const ts  = el.thumb.querySelector(".v2-thumb-ts");
    img.src  = `/api/thumb?path=${encodeURIComponent(seg.path)}&t=${off.toFixed(1)}`;
    ts.textContent = new Date(hit.ts * 1000).toLocaleTimeString(undefined,
      { hour:"2-digit", minute:"2-digit", second:"2-digit" });
    const THUMB_W = 164;
    const L = Math.max(0, Math.min(rect.width - THUMB_W, e.clientX - rect.left - THUMB_W / 2));
    el.thumb.style.left = `${L}px`;
    el.thumb.hidden = false;
  }, 80);
});
el.tlCanvas.addEventListener("mouseleave", () => {
  clearTimeout(hoverTimer);
  el.thumb.hidden = true;
  if (!st.clip.drag && !_drag) el.tlCanvas.style.cursor = "";
});

// ── Event navigation ──────────────────────────────────
function scrollTimelineToTs(ts) {
  const span = st.window.to - st.window.from;
  if (ts < st.window.from + span * 0.1 || ts > st.window.to - span * 0.1) {
    st.window.from = ts - span * 0.4;
    st.window.to   = ts + span * 0.6;
    setTimelineWindow(st.window.from, st.window.to);
  }
}

function navPrev() {
  const ts = liveTail.active ? liveTailCurrentTs() : player.reliableTs;
  if (ts == null) return;
  const evts = filteredEvts().filter(e => e.abs_ts < ts - 1).sort((a,b) => b.abs_ts - a.abs_ts);
  const evt  = evts[0];
  if (evt) {
    seekToEvent(evt);
  } else if (_eventsLoadedBounds().from > 0 && ts > _eventsLoadedBounds().from + 300) {
    // Shift window left and re-fetch to find earlier events (one retry only)
    const span = st.window.to - st.window.from;
    st.window.to   = ts - 1;
    st.window.from = st.window.to - span;
    _eventsRangesClear();
    setTimelineWindow(st.window.from, st.window.to);
    load().then(() => {
      const e2 = filteredEvts().filter(e => e.abs_ts < ts - 1).sort((a,b) => b.abs_ts - a.abs_ts)[0];
      if (!e2) return;
      seekToEvent(e2);
    });
  }
}

function navNext() {
  const ts = liveTail.active ? liveTailCurrentTs() : player.reliableTs;
  if (ts == null) return;
  const evts = filteredEvts().filter(e => e.abs_ts > ts + 1).sort((a,b) => a.abs_ts - b.abs_ts);
  const evt  = evts[0];
  if (evt) {
    seekToEvent(evt);
  } else if (_eventsLoadedBounds().to > 0 && ts < _eventsLoadedBounds().to - 300) {
    // Shift window right and re-fetch to find later events (one retry only)
    const span = st.window.to - st.window.from;
    st.window.from = ts + 1;
    st.window.to   = st.window.from + span;
    _eventsRangesClear();
    setTimelineWindow(st.window.from, st.window.to);
    load().then(() => {
      const e2 = filteredEvts().filter(e => e.abs_ts > ts + 1).sort((a,b) => a.abs_ts - b.abs_ts)[0];
      if (!e2) return;
      seekToEvent(e2);
    });
  }
}

// ── Live tail ──────────────────────────────────────────
function latestOpenSegment(srcId = null) {
  const cutoff = Date.now() / 1000 - LIVE_OPEN_MAX_AGE;
  return [...st.segments]
    .filter(s => s.end_ts == null && s.start_ts >= cutoff && (!srcId || s.source_id === srcId))
    .sort((a, b) => b.start_ts - a.start_ts)[0] ?? null;
}

function firstRtspSourceId() {
  return st.sources.find(s => s.type === "rtsp")?.id
    ?? st.sources[0]?.id
    ?? null;
}

function chooseLiveSource(srcId = null) {
  if (srcId && srcId !== "all") return srcId;
  const selected = st.source !== "all" ? st.source : null;
  if (selected) return selected;
  return firstRtspSourceId()
    ?? player.currentSeg?.source_id
    ?? latestOpenSegment()?.source_id
    ?? st.segments[0]?.source_id
    ?? null;
}

function shouldUseNativeHls() {
  const ua = navigator.userAgent || "";
  const isiOS = /iPad|iPhone|iPod/.test(ua)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const isSafari = /Safari/i.test(ua)
    && !/Chrome|Chromium|CriOS|FxiOS|Edg|OPR|Android/i.test(ua);
  return isiOS || isSafari;
}

function loadHlsJs() {
  if (window.Hls) return Promise.resolve(window.Hls);
  if (!window.__v2HlsPromise) {
    window.__v2HlsPromise = new Promise((res, rej) => {
      const s = document.createElement("script");
      s.src = "/hls.min.js";
      s.onload = () => res(window.Hls);
      s.onerror = rej;
      document.head.appendChild(s);
    });
  }
  return window.__v2HlsPromise;
}

function replaceLiveHistory(srcId, ts = null) {
  const p = new URLSearchParams({ source: srcId, live: "1" });
  const urlTs = urlTimestamp(ts);
  if (urlTs != null) p.set("ts", urlTs);
  if (st.cls.size > 0) p.set("cls", [...st.cls].join(","));
  if (st.xls.size > 0) p.set("xcls", [...st.xls].join(","));
  p.set("zone", selectedZoneParam());
  history.replaceState(null, "", `${location.pathname}?${p}`);
}

async function startLiveTail(srcId = null, options = {}) {
  const seekTs = Number.isFinite(options.seekTs) ? options.seekTs : null;
  const requestedAll = !srcId || srcId === "all";
  const chosen = chooseLiveSource(srcId);
  if (!chosen) {
    setStatus("NONE", "NO SOURCE");
    return false;
  }

  if (liveTail.active && liveTail.srcId === chosen && seekTs != null) {
    const win = await fetchLiveWindow(chosen);
    if (!liveWindowContains(win, seekTs) || !seekLiveVideoToTs(seekTs, win)) {
      setStatus("BUFFERING");
      return false;
    }
    replaceLiveHistory(chosen, seekTs);
    updateLiveTailClock();
    updateLivePlaybackRate();
    el.liveVideo.play().catch(() => {});
    return true;
  }
  if ((liveTail.active || liveTail.starting) && liveTail.srcId === chosen) return true;

  let liveWindow = null;
  if (seekTs != null) {
    liveWindow = await fetchLiveWindow(chosen);
    if (!liveWindowContains(liveWindow, seekTs)) {
      setStatus("BUFFERING");
      return false;
    }
  }

  const token = ++liveTail.token;
  liveTail.starting = true;
  liveTail.srcId = chosen;
  stopLiveTail(false, false);
  liveTail.starting = true;
  liveTail.srcId = chosen;

  if (requestedAll && st.source === "all") {
    st.source = chosen;
    renderSrcCtrl();
    timeline.setData(allSegsForSrc(), filteredEvts());
    fetchActivitySummary().then(() => {
      updateActivityCount();
      renderClsCtrl();
      renderNearScope();
      scheduleNearestEvents(true);
    });
  }

  liveTail.active = true;
  liveTail.latestDet = null;
  liveTail.recentDets = [];
  liveTail.window = liveWindow;
  liveTail.bitcTimeOffset = null;
  liveTail.targetTs = seekTs;
  mode.enterLive();
  player.pause();

  el.video.style.display = "none";
  el.empty.style.display = "none";
  el.liveVideo.style.display = "block";
  el.liveBtn.classList.add("active", "on");
  setPlayIcon(true);
  setStatus("LIVE");
  replaceLiveHistory(chosen, seekTs);

  const nativeLive = seekTs == null ? await fetchNativeLiveSource(chosen) : null;
  if (token !== liveTail.token) return false;
  const useNativeLowLatency = Boolean(nativeLive?.url);
  liveTail.syncToDetections = useNativeLowLatency && seekTs == null;
  liveTail.syncDetTs = null;
  liveTail.holdingForDetections = false;
  const hlsUrl = useNativeLowLatency
    ? nativeLive.url
    : `/video/live/${encodeURIComponent(chosen)}/live.m3u8`;

  el.liveVideo.onerror = null;  // clear before re-wiring
  el.liveVideo.onerror = e => {
    const err = el.liveVideo.error;
    console.error("liveVideo error:", err?.code, err?.message, hlsUrl);
    setStatus("OFFLINE");
  };

  async function _attachHls() {
    // Clear any stale srcObject before setting src
    if (el.liveVideo.srcObject) { el.liveVideo.srcObject = null; }
    const canNative = el.liveVideo.canPlayType("application/vnd.apple.mpegurl");
    const preferNative = Boolean(canNative && shouldUseNativeHls());
    const HlsCtor = preferNative ? null : await loadHlsJs().catch(() => null);
    const canUseHlsJs = Boolean(HlsCtor?.isSupported?.());
    console.log("HLS attach:", hlsUrl, "ll-hls:", useNativeLowLatency, "hls.js:", canUseHlsJs, "native:", !!canNative, "preferNative:", preferNative);
    if (canUseHlsJs) {
      if (token !== liveTail.token) return;
      if (liveTail.hls) { liveTail.hls.destroy(); liveTail.hls = null; }
      const hlsConfig = useNativeLowLatency
        ? { lowLatencyMode: true }
        : {
            lowLatencyMode: false,
            // Ride 2 segments back from the live edge (default 3) for lower latency.
            // liveSyncDurationCount (the sync TARGET) is safe; only the separate
            // liveMaxLatencyDurationCount triggers the init catchup bug (currentTime=0
            // → apparent latency=60s >> limit → max poll rate), so that stays omitted.
            liveSyncDurationCount: 2,
          };
      if (!useNativeLowLatency && seekTs != null && liveWindow) hlsConfig.startPosition = liveMediaOffsetForTs(liveWindow, seekTs);
      const hls = new HlsCtor(hlsConfig);
      liveTail.hls = hls;
      hls.loadSource(hlsUrl);
      hls.attachMedia(el.liveVideo);
      hls.on(HlsCtor.Events.MANIFEST_PARSED, () => {
        if (!useNativeLowLatency && seekTs != null && liveWindow) seekLiveVideoToTs(seekTs, liveWindow);
        updateLivePlaybackRate();
        el.liveVideo.play().catch(() => {});
      });
      hls.on(HlsCtor.Events.ERROR, (_, data) => {
        if (token !== liveTail.token) return;
        if (data.fatal) { console.warn("HLS fatal:", data.type, data.details); stopLiveTail(); }
      });
    } else if (canNative) {
      if (token !== liveTail.token) return;
      // Safari/iOS native HLS fallback. Desktop Chromium is kept on hls.js
      // because its native media loader requests live manifests as ranges.
      el.liveVideo.src = hlsUrl;
      el.liveVideo.load();
      el.liveVideo.addEventListener("loadedmetadata", () => {
        if (!useNativeLowLatency && seekTs != null && liveWindow) seekLiveVideoToTs(seekTs, liveWindow);
        updateLivePlaybackRate();
        el.liveVideo.play().catch(e => console.warn("play() failed:", e));
      }, { once: true });
    } else {
      throw new Error("HLS playback is not supported in this browser");
    }
  }

  try {
    await _attachHls();
    if (token !== liveTail.token) return;
    await pollLiveTail();   // self-chains its own setTimeout while active
    if (token !== liveTail.token) return;
    liveTail.clockTimer = setInterval(updateLiveTailClock, 500);
    startLiveFrameLoop();
    liveTail.starting = false;
    updateLivePlaybackRate();
    return true;
  } catch (err) {
    if (token !== liveTail.token) return;
    liveTail.starting = false;
    console.error("live HLS:", err);
    stopLiveTail();
    setStatus("OFFLINE");
    return false;
  }
}

function stopLiveTail(updateMode = true, invalidate = true) {
  if (invalidate) liveTail.token++;
  clearTimeout(liveTail.pollTimer);   // self-chained setTimeout now
  clearInterval(liveTail.clockTimer);
  if (liveTail.cancelFrame) { liveTail.cancelFrame(); liveTail.cancelFrame = null; }
  liveTail.pollTimer = null;
  liveTail.clockTimer = null;
  if (liveTail.hls) { liveTail.hls.destroy(); liveTail.hls = null; }
  if (el.liveVideo) {
    el.liveVideo.onerror = null;  // prevent stale onerror → OFFLINE when clearing src
    el.liveVideo.pause();
    setLivePlaybackRate(1);
    el.liveVideo.src = "";
  }
  if (el.liveVideo) el.liveVideo.style.display = "none";
  liveTail.active = false;
  liveTail.starting = false;
  liveTail.srcId = null;
  liveTail.latestDet = null;
  liveTail.recentDets = [];
  liveTail.recentSeq++;
  liveTail._tracklets = null;
  liveTail.syncToDetections = false;
  liveTail.syncDetTs = null;
  liveTail.holdingForDetections = false;
  liveTail.window = null;
  liveTail.bitcTimeOffset = null;
  liveTail.targetTs = null;
  // Restore URL: remove live=1 and ts params
  const _p = new URLSearchParams(location.search);
  _p.delete("live"); _p.delete("ts");
  history.replaceState(null, "", `${location.pathname}${_p.size ? "?" + _p : ""}`);
  el.liveBtn.classList.remove("active", "on");
  setPlayIcon(!player.paused);
  drawBoxList(el.video, []);
  if (updateMode) mode.stopLive();
  if (el.video.dataset.src) el.video.style.display = "block";
  else el.empty.style.display = "block";
  setStatus("REPLAY");
}

async function pollLiveTail() {
  if (!liveTail.active || !liveTail.srcId) return;
  const token = liveTail.token;   // self-chained below; never overlaps (was setInterval)
  try {
    const p = new URLSearchParams({ source: liveTail.srcId });
    const focusTs = liveTailCurrentTs();
    if (Number.isFinite(focusTs)) {
      p.set("det_since", (focusTs - LIVE_DET_WINDOW_SECONDS).toFixed(3));
      p.set("det_until", (focusTs + LIVE_DET_WINDOW_SECONDS).toFixed(3));
    }
    appendZoneParam(p);
    const r = await fetch(`/api/video/live?${p}`, { cache:"no-store" }).catch(() => null);
    if (token !== liveTail.token) return;   // stopped/restarted during fetch → drop
    if (!r?.ok) return;
    const data = await r.json();
    if (token !== liveTail.token) return;
    mergeSegments(data.segments || []);
    replaceProvisionalEvents(data.events || [], liveTail.srcId);
    mergeClassCounts(data.events || []);
    liveTail.latestDet = (data.detections || []).find(d => d.source_id === liveTail.srcId) ?? liveTail.latestDet;
    liveTail.recentDets = (data.recent_detections || []).filter(d => d.source_id === liveTail.srcId);
    liveTail.recentSeq++;   // invalidate the live tracklet cache
    syncLiveVideoToLatestBoxes();
    renderClsCtrl();
    timeline.setData(allSegsForSrc(), filteredEvts());
    scheduleNearestEvents(true);
    updateLiveTailClock();
  } finally {
    if (token === liveTail.token && liveTail.active) {
      liveTail.pollTimer = setTimeout(pollLiveTail, LIVE_DET_POLL_MS);
    }
  }
}

function updateLiveTailClock() {
  if (!liveTail.active) return;

  // Recover from paused state (tab hidden, autoplay policy, etc.)
  if (!liveTail.holdingForDetections && !st.zoneEdit.active && el.liveVideo.paused && !el.liveVideo.ended) {
    el.liveVideo.play().catch(() => {});
  }

  syncLiveVideoToLatestBoxes();
  const ts = liveTailCurrentTs();
  updateLivePlaybackRate(ts);
  timeline.setPlayhead(ts);
  setTimestampChip(ts, liveTail.srcId, true);
  setStatus("LIVE");
  drawZones();
  // box overlay is driven per displayed frame (startLiveFrameLoop), not here
}

// Draw the live overlay once per displayed video frame so interpolated boxes
// glide smoothly. requestVideoFrameCallback fires only on actual paints (and
// pauses with the tab); falls back to rAF where unsupported.
function startLiveFrameLoop() {
  const v = el.liveVideo;
  if (typeof v.requestVideoFrameCallback === "function") {
    let id;
    const onFrame = () => {
      if (!liveTail.active) return;
      drawLiveBoxes();
      id = v.requestVideoFrameCallback(onFrame);
    };
    id = v.requestVideoFrameCallback(onFrame);
    liveTail.cancelFrame = () => v.cancelVideoFrameCallback(id);
  } else {
    let id;
    const tick = () => {
      if (!liveTail.active) return;
      drawLiveBoxes();
      id = requestAnimationFrame(tick);
    };
    id = requestAnimationFrame(tick);
    liveTail.cancelFrame = () => cancelAnimationFrame(id);
  }
}

// ── Player controls ───────────────────────────────────
function togglePlayback() {
  if (liveTail.active) {
    if (el.liveVideo.paused) {
      updateLivePlaybackRate();
      el.liveVideo.play().catch(() => {});
    }
    else el.liveVideo.pause();
    setPlayIcon(!el.liveVideo.paused);
    return;
  }
  if (!player.paused) { player.pause(); return; }
  mode.playFromCurrent(st.source !== "all" ? st.source : null);
}

el.play.addEventListener("click", togglePlayback);
el.prev.addEventListener("click", navPrev);
el.next.addEventListener("click", navNext);
el.rewind.addEventListener("click", () => {
  const wasLive = liveTail.active;
  const ts  = wasLive ? liveTailCurrentTs() : player.reliableTs;
  const src = wasLive ? liveTail.srcId : (player.currentSeg?.source_id ?? null);
  if (wasLive) stopLiveTail(false);
  if (ts != null) {
    const target = ts - 10;
    seekToTimestamp(src, target, { scroll: false });
  }
});
el.loop.addEventListener("click",   () => {
  st.loop = !st.loop;
  el.loop.classList.toggle("active", st.loop);
  el.loop.classList.toggle("on", st.loop);
});
el.boxes.addEventListener("click",  () => {
  st.showBoxes = !st.showBoxes;
  localStorage.setItem("v2boxes", st.showBoxes ? "1" : "0");
  el.boxes.classList.toggle("active", st.showBoxes);
});
el.boxes.classList.toggle("active", st.showBoxes);
el.loop.classList.toggle("on", st.loop);
el.liveBtn.addEventListener("click", () => {
  if (liveTail.active) { stopLiveTail(); return; }
  startLiveTail(st.source !== "all" ? st.source : null);
  scrollTimelineToTs(Date.now() / 1000);
});

function toggleFullscreen() {
  const s = el.stage || document.querySelector(".v2-stage");
  if (!s) return;
  document.fullscreenElement ? document.exitFullscreen() : s.requestFullscreen().catch(()=>{});
}

function openClipDownloadToolbar() {
  if (st.clip.active) {
    st.clip.toolbarOpen = true;
    syncClipSelection();
  } else {
    startClipSelection({ showToolbar: true });
  }
}
function downloadCurrentFrame() {
  const v = liveTail.active ? el.liveVideo : el.video;
  if (!v || !v.videoWidth) {
    setStatus("NONE");
    return;
  }
  const c = document.createElement("canvas");
  c.width = v.videoWidth;
  c.height = v.videoHeight;
  const ctx = c.getContext("2d");
  ctx.drawImage(v, 0, 0, c.width, c.height);
  // Bake in detection boxes if currently shown. The box canvas renders the
  // image letterboxed inside its display rect; crop to that rect and stretch
  // to native frame size so boxes line up.
  const bc = el.canvas;
  if (st.showBoxes && bc?.width) {
    const scale = Math.min(bc.width / c.width, bc.height / c.height);
    const rw = c.width * scale, rh = c.height * scale;
    const ox = (bc.width - rw) / 2, oy = (bc.height - rh) / 2;
    ctx.drawImage(bc, ox, oy, rw, rh, 0, 0, c.width, c.height);
  }
  // Use a data: URL rather than a blob: URL — browsers flag blob downloads
  // over plain HTTP as insecure, but data: URLs download fine.
  const ts = liveTail.active ? liveTailCurrentTs() : player.reliableTs;
  const a = document.createElement("a");
  a.href = c.toDataURL("image/png");
  a.download = `frame-${Math.floor(ts || Date.now() / 1000)}.png`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}
el.fullscreen?.addEventListener("click", toggleFullscreen);
el.download?.addEventListener("click", openClipDownloadToolbar);
el.clipPreview?.addEventListener("click", previewSelectedClip);
el.clipDownload?.addEventListener("click", downloadSelectedClip);
el.clipCancel?.addEventListener("click", cancelClipSelection);
el.downloadFrame?.addEventListener("click", downloadCurrentFrame);
el.activityToggle?.addEventListener("click", () => {
  setActivityOpen(!st.activityOpen);
});
el.activityBackdrop?.addEventListener("click", () => {
  setActivityOpen(false);
});
el.notifyToggle?.addEventListener("click", (e) => {
  e.stopPropagation();
  setNotificationsOpen(!st.notificationsOpen);
});
el.notifyPanel?.addEventListener("click", e => {
  e.stopPropagation();
});
el.notifyReadAll?.addEventListener("click", markAllNotificationsRead);
if (activityDrawerMq?.addEventListener) {
  activityDrawerMq.addEventListener("change", syncActivityDrawerMode);
} else if (activityDrawerMq?.addListener) {
  activityDrawerMq.addListener(syncActivityDrawerMode);
}
syncActivityDrawerMode();

// Speed pills
function buildSpeedPills() {
  el.speeds.innerHTML = "";
  V2_SPEEDS.forEach((s, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "speed-pill" + (i === st.speed ? " active" : "");
    b.textContent = s.label;
    b.addEventListener("click", () => { setPlaybackSpeed(i); });
    el.speeds.appendChild(b);
  });
}

function setPlaybackSpeed(idx) {
  const speed = V2_SPEEDS[idx];
  if (!speed) return;
  st.speed = idx;
  localStorage.setItem("v2speed", idx);
  player.setRate(speed.rate);
  updateLivePlaybackRate();
  buildSpeedPills();
}

// Keyboard
document.addEventListener("keydown", e => {
  if (["INPUT","TEXTAREA","SELECT"].includes(e.target.tagName)) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;  // don't steal browser/OS shortcuts
  if (e.key === " ")           { e.preventDefault(); togglePlayback(); }
  if (e.key === "ArrowLeft" && e.shiftKey) {
    e.preventDefault();
    el.rewind.click();
    return;
  }
  if (e.key === "ArrowLeft")  { e.preventDefault(); navPrev(); }
  if (e.key === "ArrowRight") { e.preventDefault(); navNext(); }
  if (e.key.toLowerCase() === "l") { e.preventDefault(); el.loop.click(); }
  if (e.key.toLowerCase() === "b") { e.preventDefault(); el.boxes.click(); }
  if (["1", "2", "3", "4"].includes(e.key)) {
    e.preventDefault();
    const idx = Number(e.key) - 1;
    setPlaybackSpeed(idx);
  }
});
el.video.addEventListener("click",    togglePlayback);
el.liveVideo.addEventListener("click", togglePlayback);
el.video.addEventListener("dblclick", toggleFullscreen);
el.liveVideo.addEventListener("dblclick", toggleFullscreen);

el.zones?.addEventListener("click", (e) => {
  e.stopPropagation();
  if (st.zoneEdit.active) { cancelZoneEditor(); return; }
  toggleZoneMenu();
});
document.addEventListener("click", (e) => {
  if (st.notificationsOpen && !el.notifyPanel?.contains(e.target) && !el.notifyToggle?.contains(e.target)) {
    setNotificationsOpen(false);
  }
  if (!el.zoneMenu || el.zoneMenu.hidden) return;
  if (el.zonePicker?.contains(e.target)) return;
  closeZoneMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && st.activityOpen) setActivityOpen(false);
  if (e.key === "Escape" && st.notificationsOpen) setNotificationsOpen(false);
  if (e.key === "Escape" && el.zoneMenu && !el.zoneMenu.hidden) closeZoneMenu();
});
el.zonePrev?.addEventListener("click", () => selectZone(st.zoneEdit.selected - 1));
el.zoneNext?.addEventListener("click", () => selectZone(st.zoneEdit.selected + 1));
el.zoneNew?.addEventListener("click", addZoneDraft);
el.zoneDelete?.addEventListener("click", deleteSelectedZoneDraft);
el.zoneSave?.addEventListener("click", saveZoneEditor);
el.zoneReset?.addEventListener("click", resetZoneEditor);
el.zoneCancel?.addEventListener("click", cancelZoneEditor);
el.zoneName?.addEventListener("input", () => {
  const z = selectedDraftZone();
  if (z) z.name = el.zoneName.value.slice(0, 80);
});

el.zoneCanvas?.addEventListener("pointerdown", e => {
  if (!st.zoneEdit.active || e.button !== 0) return;
  e.preventDefault();
  const pt = canvasToNorm(e);
  if (!pt) return;
  const hit = zonePointAt(e);
  if (hit != null) {
    st.zoneEdit.dragPoint = hit;
  } else {
    let points = selectedPoints();
    const draftingZone = !selectedDraftZone() || points.length < 3;
    if (!draftingZone) {
      const clickedZone = zoneUnderPointer(e);
      if (clickedZone != null && clickedZone !== st.zoneEdit.selected) {
        selectZone(clickedZone);
        points = selectedPoints();
      }
    }
    const nearEdge = zoneEdgeAt(e, 14);
    if (nearEdge != null) {
      points.splice(nearEdge + 1, 0, pt);
      st.zoneEdit.dragPoint = nearEdge + 1;
    } else if (points.length >= 3 && pointInPoly(pt, points)) {
      st.zoneEdit.dragPoly = true;
      st.zoneEdit.last = pt;
    } else {
      const edge = zoneEdgeAt(e);
      if (edge != null && points.length >= 2) {
        points.splice(edge + 1, 0, pt);
        st.zoneEdit.dragPoint = edge + 1;
      } else {
        points = ensureDraftZone().polygon;
        points.push(pt);
        st.zoneEdit.dragPoint = points.length - 1;
      }
    }
  }
  el.zoneCanvas.setPointerCapture?.(e.pointerId);
  updateZoneChrome();
  drawZones();
});

el.zoneCanvas?.addEventListener("pointermove", e => {
  if (!st.zoneEdit.active) return;
  const pt = canvasToNorm(e);
  if (!pt) return;
  if (st.zoneEdit.dragPoint != null) {
    selectedPoints()[st.zoneEdit.dragPoint] = pt;
    drawZones();
  } else if (st.zoneEdit.dragPoly && st.zoneEdit.last) {
    moveZonePolygon(pt.x - st.zoneEdit.last.x, pt.y - st.zoneEdit.last.y);
    st.zoneEdit.last = pt;
    drawZones();
  }
});

el.zoneCanvas?.addEventListener("pointerup", e => {
  if (!st.zoneEdit.active) return;
  st.zoneEdit.dragPoint = null;
  st.zoneEdit.dragPoly = false;
  st.zoneEdit.last = null;
  el.zoneCanvas.releasePointerCapture?.(e.pointerId);
});

el.zoneCanvas?.addEventListener("dblclick", e => {
  if (!st.zoneEdit.active) return;
  const hit = zonePointAt(e);
  if (hit == null) return;
  selectedPoints().splice(hit, 1);
  updateZoneChrome();
  drawZones();
});

// ── Player events → UI ────────────────────────────────
player.on("play",  () => { if (!liveTail.active) setPlayIcon(true); });
player.on("pause", () => { if (!liveTail.active) setPlayIcon(false); });
player.on("ended", () => {
  if (st.clip.previewing) {
    maybeLoopResolvedClipPreview(player.currentTs ?? st.clip.end);
    return;
  }
  mode.handleEnded(st.source !== "all" ? st.source : null);
});

player.on("timeupdate", () => {
  const ts = player.currentTs;
  if (ts == null) return;
  timeline.setPlayhead(ts);
  setTimestampChip(ts, player.currentSeg?.source_id ?? null, false);
  drawBoxes(ts);
  drawZones();
  scheduleNearestEvents();
  maybeLoopResolvedClipPreview(ts);
});

player.on("frame", () => {
  if (liveTail.active) return;
  const ts = player.currentTs;
  if (ts == null) return;
  drawBoxes(ts);
});

// ── Box overlay ───────────────────────────────────────
function overlayRangeFor(seg, ts) {
  const start = Number(seg?.start_ts);
  const end = Number(seg?.end_ts);
  if (Number.isFinite(start) && Number.isFinite(end) && end > start && end - start <= 900) {
    return { from: start - 2, to: end + 2 };
  }
  return { from: ts - 30, to: ts + 30 };
}

function overlayCacheCovers(sourceId, ts) {
  return (
    st.overlays.sourceId === sourceId &&
    Number(st.overlays.from) <= ts &&
    Number(st.overlays.to) >= ts
  );
}

async function loadOverlayDets(sourceId, from, to) {
  if (!sourceId || sourceId === "all") return;
  const lo = Math.floor(Math.min(from, to));
  const hi = Math.ceil(Math.max(from, to));
  const key = `${sourceId}|${lo}|${hi}`;
  if (st.overlays.loadingKey === key) return;
  if (
    st.overlays.sourceId === sourceId &&
    st.overlays.from <= lo &&
    st.overlays.to >= hi
  ) return;
  st.overlays.loadingKey = key;
  const seq = ++st.overlays.seq;
  const p = new URLSearchParams({
    source: sourceId,
    since: String(lo),
    until: String(hi),
  });
  const r = await fetch(`/api/video/overlays?${p}`, { cache:"no-store" }).catch(() => null);
  if (seq !== st.overlays.seq) return;
  if (r?.ok) {
    st.overlays = {
      sourceId,
      from: lo,
      to: hi,
      detections: (await r.json().catch(() => ({}))).detections || [],
      loadingKey: null,
      seq,
    };
    drawBoxes(player.currentTs);
  } else if (st.overlays.loadingKey === key) {
    st.overlays.loadingKey = null;
  }
}

function boxCenter(box) {
  return {
    x: (Number(box.x1) + Number(box.x2)) / 2,
    y: (Number(box.y1) + Number(box.y2)) / 2,
  };
}

function boxCenterDistance(a, b) {
  const ac = boxCenter(a);
  const bc = boxCenter(b);
  return Math.hypot(ac.x - bc.x, ac.y - bc.y);
}

function lerp(a, b, t) {
  return Number(a) + (Number(b) - Number(a)) * t;
}

function interpolateBox(a, b, t) {
  return {
    ...a,
    x1: lerp(a.x1, b.x1, t),
    y1: lerp(a.y1, b.y1, t),
    x2: lerp(a.x2, b.x2, t),
    y2: lerp(a.y2, b.y2, t),
    conf: lerp(a.conf ?? b.conf ?? 0, b.conf ?? a.conf ?? 0, t),
    cls: a.cls,
  };
}

// Overlay association: chain per-frame boxes into short tracklets so a box only
// interpolates toward the SAME object. Greedy nearest-center glides one box onto
// a different object across the frame (busy road). Two cheap, ML-free gates fix it:
//   mutual-NN  — link a->b only if b is a's nearest AND a is b's nearest (no hijack)
//   CV-gate    — predict next pos from velocity; reject if the miss (2nd derivative)
//                is implausible. Fast-but-straight stays; teleport/heading-flip cut.
const _OVL_MAX_GAP    = 2.5;   // s — never bridge a bigger hole
const _OVL_SNAP       = 0.8;   // s — show a lone/edge sample within this window
const _OVL_GATE_FLOOR = 0.22;  // normalized center units — generous; keep glides smooth
const _OVL_GATE_K     = 2.5;   // gate grows with speed*dt (fast straight movers get slack)
const _OVL_WARM_GATE  = 0.40;  // first link has no velocity — near old greedy, lets fast tracks start
const _OVL_MIN_SPEED  = 0.04;  // below this (per s) treat as stationary; skip heading veto
const _OVL_LEAD       = 0.15;  // s — tiny forward tolerance so the current frame still shows

// Pure: chain a detection list into tracklets. Shared by the recorded overlay
// (cached on st.overlays.seq) and the live overlay (cached on liveTail.recentSeq).
function buildTracklets(detections) {
  const samples = (detections || [])
    .map(d => ({ ts: Number(d.abs_ts), boxes: (d.boxes || []) }))   // unfiltered; filtered at draw
    .filter(s => Number.isFinite(s.ts) && s.boxes.length)
    .sort((a, b) => a.ts - b.ts);
  const tracks = [];   // {cls, vx, vy, pts:[{ts,box,cx,cy}]}
  for (const s of samples) {
    const heads = tracks.filter(t => s.ts - t.pts[t.pts.length - 1].ts <= _OVL_MAX_GAP);
    const cands = s.boxes.map(box => {
      const c = boxCenter(box);
      return { box, cx: c.x, cy: c.y, cls: box.cls, used: false, bestHead: null };
    });
    const preds = heads.map(t => {
      const h = t.pts[t.pts.length - 1];
      const dt = s.ts - h.ts;
      const moving = t.vx != null;
      const px = moving ? h.cx + t.vx * dt : h.cx;
      const py = moving ? h.cy + t.vy * dt : h.cy;
      const gate = moving ? Math.max(_OVL_GATE_FLOOR, _OVL_GATE_K * Math.hypot(t.vx, t.vy) * dt)
                          : _OVL_WARM_GATE;
      return { t, h, dt, px, py, gate, best: null, bestDist: Infinity };
    });
    // forward: each head's best candidate = min residual to its CV prediction
    for (const p of preds)
      for (const cand of cands) {
        if (cand.cls !== p.t.cls) continue;
        const d = Math.hypot(cand.cx - p.px, cand.cy - p.py);
        if (d < p.bestDist) { p.bestDist = d; p.best = cand; }
      }
    // backward: each candidate's best head = nearest head center (same class)
    for (const cand of cands) {
      let bd = Infinity;
      for (const p of preds) {
        if (p.t.cls !== cand.cls) continue;
        const d = Math.hypot(cand.cx - p.h.cx, cand.cy - p.h.cy);
        if (d < bd) { bd = d; cand.bestHead = p; }
      }
    }
    // link only mutual pairs that pass the gate (and don't reverse heading)
    for (const p of preds) {
      const b = p.best;
      if (!b || b.used || b.bestHead !== p || p.bestDist > p.gate) continue;
      // heading veto: an established mover can't flip direction >90deg in one step
      // (across-street hijack is opposite to the subject's motion). Magnitude alone
      // can't tell — a fast straight mover needs a big gate, which also admits the
      // stranger; direction is the discriminator.
      if (p.t.vx != null && Math.hypot(p.t.vx, p.t.vy) > _OVL_MIN_SPEED) {
        const ddx = b.cx - p.h.cx, ddy = b.cy - p.h.cy;
        if (p.t.vx * ddx + p.t.vy * ddy < 0) continue;   // reversal -> reject
      }
      p.t.vx = (b.cx - p.h.cx) / p.dt;
      p.t.vy = (b.cy - p.h.cy) / p.dt;
      p.t.pts.push({ ts: s.ts, box: b.box, cx: b.cx, cy: b.cy });
      b.used = true;
    }
    // unmatched detections start their own tracklet
    for (const cand of cands)
      if (!cand.used)
        tracks.push({ cls: cand.cls, vx: null, vy: null,
                      pts: [{ ts: s.ts, box: cand.box, cx: cand.cx, cy: cand.cy }] });
  }
  return tracks;
}

function overlayTracklets() {
  const o = st.overlays;
  if (o._tracklets && o._trackletsSeq === o.seq) return o._tracklets;
  const tracks = buildTracklets(o.detections || []);
  o._tracklets = tracks;
  o._trackletsSeq = o.seq;
  return tracks;
}

function liveTracklets() {
  const o = liveTail;
  if (o._tracklets && o._trackletsSeq === o.recentSeq) return o._tracklets;
  const tracks = buildTracklets(o.recentDets || []);
  o._tracklets = tracks;
  o._trackletsSeq = o.recentSeq;
  return tracks;
}

// Sample tracklet boxes at time ts: interpolate within a bracketed span, else
// persist the most recent PAST detection (causal). Unfiltered — caller filters.
function boxesFromTracklets(tracks, ts) {
  const out = [];
  for (const t of tracks) {
    const pts = t.pts;
    let drawn = false;
    for (let i = 0; i + 1 < pts.length; i++) {
      const a = pts[i], b = pts[i + 1];
      if (ts >= a.ts && ts <= b.ts && (b.ts - a.ts) <= _OVL_MAX_GAP) {
        out.push(interpolateBox(a.box, b.box, (ts - a.ts) / ((b.ts - a.ts) || 1)));
        drawn = true; break;
      }
    }
    if (drawn) continue;                       // interpolated within the tracklet
    // else persist the most recent PAST detection (causal): a box must never
    // appear before its own detection, or it looks like the box predicts the
    // object and the subject "rides into" a box already sitting ahead.
    let recent = null;
    for (const p of pts) {
      const age = ts - p.ts;                   // >0 = past, small <0 = current frame
      if (age >= -_OVL_LEAD && age <= _OVL_SNAP && (!recent || p.ts > recent.ts)) recent = p;
    }
    if (recent) out.push(recent.box);
  }
  return out;
}

function boxesAtOverlayTime(ts) {
  if (!Number.isFinite(Number(ts))) return [];
  return _filterBoxes(boxesFromTracklets(overlayTracklets(), ts));   // class/zone filter at draw
}

// ── BITC marker: read the world-time burned into the displayed frame ──────────
// JS port of wanyard.bitc.decode. The marker is a bottom-flush, bottom-left strip
// of 46 8px black/white cells: 38 payload bits (unix centiseconds, LSB first) +
// 8 CRC bits. Read at NATIVE resolution so the fixed-pixel strip is exact.
const _BITC_CELL = 8, _BITC_NCELLS = 46, _BITC_NPAY = 38, _BITC_W = _BITC_CELL * _BITC_NCELLS;
let _bitcCrcTable = null, _bitcCanvas = null;
function _bitcCrc32(bytes) {
  if (!_bitcCrcTable) {
    _bitcCrcTable = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
      _bitcCrcTable[n] = c >>> 0;
    }
  }
  let crc = 0xFFFFFFFF;
  for (let i = 0; i < bytes.length; i++) crc = (_bitcCrcTable[(crc ^ bytes[i]) & 0xFF] ^ (crc >>> 8)) >>> 0;
  return (crc ^ 0xFFFFFFFF) >>> 0;
}
function decodeLiveMarker(v) {
  const iw = v.videoWidth, ih = v.videoHeight;
  if (!iw || !ih || ih < _BITC_CELL || iw < _BITC_W) return null;
  if (!_bitcCanvas) { _bitcCanvas = document.createElement("canvas"); _bitcCanvas.width = _BITC_W; _bitcCanvas.height = _BITC_CELL; }
  const ctx = _bitcCanvas.getContext("2d", { willReadFrequently: true });
  let data;
  try {
    ctx.drawImage(v, 0, ih - _BITC_CELL, _BITC_W, _BITC_CELL, 0, 0, _BITC_W, _BITC_CELL);
    data = ctx.getImageData(0, 0, _BITC_W, _BITC_CELL).data;
  } catch (e) { return null; }                 // not yet decoded / tainted
  const pad = (_BITC_CELL / 4) | 0;            // inner-50% sampling
  const bits = new Array(_BITC_NCELLS);
  for (let i = 0; i < _BITC_NCELLS; i++) {
    let sum = 0, cnt = 0;
    for (let y = pad; y < _BITC_CELL - pad; y++)
      for (let x = i * _BITC_CELL + pad; x < i * _BITC_CELL + _BITC_CELL - pad; x++) {
        const idx = (y * _BITC_W + x) * 4;
        sum += data[idx] + data[idx + 1] + data[idx + 2]; cnt += 3;
      }
    bits[i] = (sum / cnt) > 128 ? 1 : 0;
  }
  let value = 0;                                // 38 bits: arithmetic (>32-bit)
  for (let i = 0; i < _BITC_NPAY; i++) if (bits[i]) value += Math.pow(2, i);
  let crc = 0;
  for (let i = 0; i < 8; i++) if (bits[_BITC_NPAY + i]) crc += (1 << i);
  const vb = new Array(8); let vv = value;
  for (let j = 0; j < 8; j++) { vb[j] = vv % 256; vv = Math.floor(vv / 256); }
  if ((_bitcCrc32(vb) & 0xFF) !== crc) return null;
  return value / 100;                          // unix seconds
}
function _maskBitcStrip(ctx, c, v) {
  const iw = v.videoWidth, ih = v.videoHeight;
  if (!iw || !ih) return;
  const scale = Math.min(c.width / iw, c.height / ih);
  const rw = iw * scale, rh = ih * scale, ox = (c.width - rw) / 2, oy = (c.height - rh) / 2;
  const sx = ox, sy = oy + (1 - _BITC_CELL / ih) * rh;
  const sw = (_BITC_W / iw) * rw, sh = (_BITC_CELL / ih) * rh;
  ctx.fillStyle = "#0b0e12";
  ctx.fillRect(sx, sy - 1, sw + 1, sh + 2);    // small pad to fully cover
}

function drawBoxes(ts) {
  if (liveTail.active) return;
  const v = el.video;
  const seg = player.currentSeg;
  if (!Number.isFinite(Number(ts))) { drawBoxList(v, []); return; }
  if (!seg) { drawBoxList(v, []); return; }
  const sourceId = seg.source_id ?? (st.source !== "all" ? st.source : null);
  if (!sourceId) { drawBoxList(v, []); return; }
  if (!overlayCacheCovers(sourceId, ts)) {
    const range = overlayRangeFor(seg, ts);
    loadOverlayDets(sourceId, range.from, range.to);
    drawBoxList(v, []);
    return;
  }

  drawBoxList(v, boxesAtOverlayTime(ts));
}

function drawLiveBoxes() {
  // The displayed frame carries its exact BITC time in the burned marker. HLS
  // playback is held near the freshest YOLO timestamp, so recentDets should
  // bracket the marker time or land exactly on the latest boxes.
  const markerTs = decodeLiveMarker(el.liveVideo);
  if (markerTs == null) { drawBoxList(el.liveVideo, []); return; }
  let boxes = liveTail.recentDets.length ? boxesFromTracklets(liveTracklets(), markerTs) : [];
  if (!boxes.length) {                       // gap fallback: nearest detection within 1s
    let best = null, bestDist = Infinity;
    for (const d of liveTail.recentDets) {
      const dist = Math.abs(d.abs_ts - markerTs);
      if (dist < bestDist) { best = d; bestDist = dist; }
    }
    const latest = latestLiveBoxDetection();
    if (latest) {
      const dist = Math.abs(Number(latest.abs_ts) - markerTs);
      if (dist < bestDist) { best = latest; bestDist = dist; }
    }
    if (best && bestDist < 1.0) boxes = best.boxes || [];
  }
  drawBoxList(el.liveVideo, _filterBoxes(boxes));
}

function _filterBoxes(boxes) {
  let b = boxes || [];
  if (st.xls.size > 0) b = b.filter(x => !st.xls.has(x.cls));
  if (st.cls.size > 0) b = b.filter(x =>  st.cls.has(x.cls));
  const polys = activeZonePolygons();
  if (polys.length) {
    b = b.filter(x => {
      const cx = (Number(x.x1) + Number(x.x2)) / 2;
      const cy = (Number(x.y1) + Number(x.y2)) / 2;
      return polys.some(poly => pointInPoly({ x: cx, y: cy }, poly));
    });
  }
  return b;
}

function activeZonePolygons() {
  if (st.activeZoneId == null) return [];
  if (st.activeZoneId === ZONE_ALL) {
    const sourceId = st.source !== "all" ? st.source : null;
    return completedActivityZones()
      .filter(z => !sourceId || z.source_id === sourceId)
      .map(z => z.polygon);
  }
  const zone = (st.zones || []).find(z => z.id === st.activeZoneId);
  if (!zone || !Array.isArray(zone.polygon) || zone.polygon.length < 3) return [];
  return [zone.polygon];
}

function drawBoxList(v, boxes) {
  const c = el.canvas;
  c.width = c.clientWidth; c.height = c.clientHeight;
  const ctx = c.getContext("2d");
  ctx.clearRect(0, 0, c.width, c.height);
  if (!v.videoWidth) return;
  _maskBitcStrip(ctx, c, v);              // hide the burned timecode strip (always)
  if (!st.showBoxes) return;
  if (!boxes.length) return;
  const cw = c.width, ch = c.height;
  const iw = v.videoWidth, ih = v.videoHeight;
  const scale = Math.min(cw/iw, ch/ih);
  const rw = iw*scale, rh = ih*scale;
  const ox = (cw-rw)/2, oy = (ch-rh)/2;

  boxes.forEach(box => {
    const primary = st.cls.size === 0 || st.cls.has(box.cls);
    const color   = classColor(box.cls);
    const x = ox+box.x1*rw, y = oy+box.y1*rh;
    const w = (box.x2-box.x1)*rw, h = (box.y2-box.y1)*rh;
    ctx.globalAlpha = primary ? 1 : 0.55;
    ctx.strokeStyle = color; ctx.lineWidth = primary ? 2.5 : 1;
    ctx.strokeRect(x,y,w,h);
    if (primary) {
      const lbl = `${box.cls} ${Math.round(box.conf*100)}%`;
      ctx.font = "bold 11px 'IBM Plex Mono',monospace";
      const tw = ctx.measureText(lbl).width+6;
      const ty = y>18?y-18:y+h;
      ctx.fillStyle=color; ctx.fillRect(x-1,ty,tw,16);
      ctx.globalAlpha=1; ctx.fillStyle="#050709";
      ctx.fillText(lbl,x+2,ty+11);
    }
  });
  ctx.globalAlpha=1;
}

// ── Activity areas ───────────────────────────────────
function isActivityZone(z) {
  return z && ["activity_area", "vehicle_event"].includes(z.type)
    && z.enabled !== false
    && Array.isArray(z.polygon);
}

function activityZones() {
  return (st.zones || []).filter(isActivityZone);
}

function completedActivityZones() {
  return activityZones().filter(z => z.polygon.length >= 3);
}

function normalizeDraftZone(zone, idx) {
  return {
    id: zone?.id,
    uid: zone?.uid,
    name: zone?.name || `Area ${idx + 1}`,
    type: "activity_area",
    enabled: zone?.enabled !== false,
    polygon: Array.isArray(zone?.polygon)
      ? zone.polygon.map(p => ({ x: Number(p.x), y: Number(p.y) }))
      : [],
  };
}

function selectedDraftZone() {
  return st.zoneEdit.zones[st.zoneEdit.selected] || null;
}

function selectedPoints() {
  return selectedDraftZone()?.polygon || [];
}

function ensureDraftZone() {
  if (selectedDraftZone()) return selectedDraftZone();
  const zone = normalizeDraftZone(null, st.zoneEdit.zones.length);
  st.zoneEdit.zones.push(zone);
  st.zoneEdit.selected = st.zoneEdit.zones.length - 1;
  updateZoneChrome();
  return zone;
}

function updateZoneControl() {
  if (el.zones) el.zones.disabled = false;
  renderZonePicker();
  drawZones();
}

function activeZoneName() {
  if (st.activeZoneId == null) return "Whole frame";
  if (st.activeZoneId === ZONE_ALL) return "All activity areas";
  const z = (st.zones || []).find(z => z.id === st.activeZoneId);
  return z?.name || `Area ${st.activeZoneId}`;
}

function renderZonePicker() {
  if (!el.zones) return;
  const singleSource = st.source !== "all";
  const zones = completedActivityZones();

  // "All" mode with no zones anywhere → nothing to pick.
  if (!singleSource && zones.length === 0) {
    if (el.zonePicker) el.zonePicker.hidden = true;
    if (st.activeZoneId != null) { st.activeZoneId = null; pushState(); }
    closeZoneMenu();
    return;
  }
  if (el.zonePicker) el.zonePicker.hidden = false;

  const validIds = new Set(zones.map(z => z.id));
  if (st.activeZoneId === ZONE_ALL && (!singleSource || zones.length === 0)) {
    st.activeZoneId = null;
    pushState();
  }
  if (st.activeZoneId != null && st.activeZoneId !== ZONE_ALL && !validIds.has(st.activeZoneId)) {
    st.activeZoneId = null;
    pushState();
  }
  if (el.zoneTriggerLabel) el.zoneTriggerLabel.textContent = activeZoneName();
  if (!el.zoneMenu) return;

  const srcNames = {};
  st.sources.forEach(s => srcNames[s.id] = s.name || s.id);

  el.zoneMenu.innerHTML = "";
  const items = [{ id: null, source: null, name: "Whole frame" }];
  if (singleSource && zones.length > 0) {
    items.push({ id: ZONE_ALL, source: null, name: "All activity areas" });
  }
  items.push(...zones.map(z => ({
    id: z.id,
    source: z.source_id,
    name: singleSource
      ? (z.name || `Area ${z.id}`)
      : `${srcNames[z.source_id] || z.source_id} · ${z.name || `Area ${z.id}`}`,
  })));
  items.forEach(t => {
    const b = document.createElement("button");
    b.type = "button";
    b.role = "menuitem";
    b.className = "st-menu-item" + (st.activeZoneId === t.id ? " active" : "");
    const name = document.createElement("span");
    name.className = "st-menu-name";
    name.textContent = t.name;
    b.appendChild(name);
    if (st.activeZoneId === t.id) {
      const tick = document.createElement("span");
      tick.textContent = "✓";
      b.appendChild(tick);
    }
    b.addEventListener("click", () => { closeZoneMenu(); setActiveZone(t.id, t.source); });
    el.zoneMenu.appendChild(b);
  });

  // Editing is per-camera; only offer it when a single source is selected.
  if (!singleSource) return;
  if (zones.length > 0) {
    const div = document.createElement("div");
    div.className = "st-menu-divider";
    el.zoneMenu.appendChild(div);
  }
  const edit = document.createElement("button");
  edit.type = "button";
  edit.role = "menuitem";
  edit.className = "st-menu-item st-menu-edit";
  edit.textContent = zones.length ? "Edit areas" : "Add area";
  edit.addEventListener("click", () => { closeZoneMenu(); startZoneEditor(); });
  el.zoneMenu.appendChild(edit);
}

function toggleZoneMenu() {
  if (!el.zoneMenu) return;
  if (el.zoneMenu.hidden) openZoneMenu();
  else closeZoneMenu();
}

function openZoneMenu() {
  if (!el.zoneMenu) return;
  renderZonePicker();
  el.zoneMenu.hidden = false;
  el.zones?.setAttribute("aria-expanded", "true");
  positionZoneMenu();
}

function positionZoneMenu() {
  if (!el.zoneMenu || !el.zones) return;
  const r = el.zones.getBoundingClientRect();
  el.zoneMenu.style.top = `${r.bottom + 6}px`;
  const mw = el.zoneMenu.offsetWidth || 200;
  const right = Math.max(8, window.innerWidth - r.right);
  el.zoneMenu.style.right = `${right}px`;
  el.zoneMenu.style.left = "auto";
  el.zoneMenu.style.minWidth = `${Math.max(r.width, mw)}px`;
}

window.addEventListener("resize", () => {
  if (el.zoneMenu && !el.zoneMenu.hidden) positionZoneMenu();
});
window.addEventListener("scroll", () => {
  if (el.zoneMenu && !el.zoneMenu.hidden) positionZoneMenu();
}, true);

function closeZoneMenu() {
  if (!el.zoneMenu) return;
  el.zoneMenu.hidden = true;
  el.zones?.setAttribute("aria-expanded", "false");
}

function setActiveZone(id, sourceId = null) {
  // Picking a zone from another camera (e.g. in "All" mode) switches to it.
  if (sourceId && sourceId !== st.source) {
    const wasLive = liveTail.active;
    cancelZoneEditor();
    stopLiveTail(false);
    st.source = sourceId; st.initDone = false;
    st.events = []; st.zones = []; st.zonesSource = null; st.activeZoneId = id;
    _eventsRangesClear();
    renderSrcCtrl();
    load().then(pushState);
    if (wasLive) startLiveTail(sourceId);
    return;
  }
  if (st.activeZoneId === id) return;
  st.activeZoneId = id;
  pushState();
  renderZonePicker();
  drawZones();
  st.events = [];
  _eventsRangesClear();
  load();
}

function activeStageVideo() {
  return liveTail.active ? el.liveVideo : el.video;
}

function videoRenderRect(v, c) {
  if (!v?.videoWidth || !v?.videoHeight || !c?.clientWidth || !c?.clientHeight) return null;
  const cw = c.clientWidth, ch = c.clientHeight;
  const scale = Math.min(cw / v.videoWidth, ch / v.videoHeight);
  const w = v.videoWidth * scale, h = v.videoHeight * scale;
  return { x: (cw - w) / 2, y: (ch - h) / 2, w, h };
}

function normToCanvas(pt) {
  const rect = videoRenderRect(activeStageVideo(), el.zoneCanvas);
  if (!rect) return null;
  return { x: rect.x + pt.x * rect.w, y: rect.y + pt.y * rect.h };
}

function canvasToNorm(evt) {
  const c = el.zoneCanvas;
  const rect = videoRenderRect(activeStageVideo(), c);
  if (!rect) return null;
  const box = c.getBoundingClientRect();
  const x = evt.clientX - box.left;
  const y = evt.clientY - box.top;
  return {
    x: Math.max(0, Math.min(1, (x - rect.x) / rect.w)),
    y: Math.max(0, Math.min(1, (y - rect.y) / rect.h)),
  };
}

function zonePointAt(evt) {
  const c = el.zoneCanvas;
  const box = c.getBoundingClientRect();
  const x = evt.clientX - box.left;
  const y = evt.clientY - box.top;
  let best = null, bestDist = 12;
  selectedPoints().forEach((pt, idx) => {
    const p = normToCanvas(pt);
    if (!p) return;
    const dist = Math.hypot(p.x - x, p.y - y);
    if (dist <= bestDist) { best = idx; bestDist = dist; }
  });
  return best;
}

function zoneEdgeAt(evt, maxDist = Infinity) {
  const pts = selectedPoints().map(normToCanvas);
  if (pts.length < 2 || pts.some(p => !p)) return null;
  const c = el.zoneCanvas;
  const box = c.getBoundingClientRect();
  const p = { x: evt.clientX - box.left, y: evt.clientY - box.top };
  let best = null, bestDist = maxDist;
  const edgeCount = pts.length >= 3 ? pts.length : pts.length - 1;
  for (let i = 0; i < edgeCount; i++) {
    const a = pts[i];
    const b = pts[(i + 1) % pts.length];
    const dist = distToSegment(p, a, b);
    if (dist < bestDist) {
      best = i;
      bestDist = dist;
    }
  }
  return best;
}

function zoneUnderPointer(evt) {
  const pt = canvasToNorm(evt);
  if (!pt) return null;
  let best = null;
  st.zoneEdit.zones.forEach((zone, idx) => {
    if ((zone.polygon || []).length >= 3 && pointInPoly(pt, zone.polygon)) best = idx;
  });
  return best;
}

function distToSegment(p, a, b) {
  const dx = b.x - a.x, dy = b.y - a.y;
  const lenSq = dx * dx + dy * dy;
  if (!lenSq) return Math.hypot(p.x - a.x, p.y - a.y);
  const t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / lenSq));
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}

function drawZones() {
  const c = el.zoneCanvas;
  if (!c) return;
  c.width = c.clientWidth; c.height = c.clientHeight;
  const ctx = c.getContext("2d");
  ctx.clearRect(0, 0, c.width, c.height);

  if (!st.zoneEdit.active) {
    const outlines = activeZonePolygons()
      .map(poly => poly.map(normToCanvas).filter(Boolean))
      .filter(pts => pts.length >= 3);
    if (!outlines.length) return;

    // dim everything outside the polygon (evenodd: outer rect minus poly)
    ctx.fillStyle = "rgba(0, 0, 0, 0.38)";
    ctx.beginPath();
    ctx.rect(0, 0, c.width, c.height);
    outlines.forEach(pts => {
      ctx.moveTo(pts[0].x, pts[0].y);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
      ctx.closePath();
    });
    ctx.fill("evenodd");

    // dark halo under the bright stroke so it reads against any background
    const drawOutline = () => {
      ctx.beginPath();
      outlines.forEach(pts => {
        pts.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
        ctx.closePath();
      });
      ctx.stroke();
    };
    ctx.setLineDash([]);
    ctx.lineWidth = 4;
    ctx.strokeStyle = "rgba(0, 0, 0, 0.7)";
    drawOutline();
    ctx.setLineDash([8, 5]);
    ctx.lineWidth = 2;
    ctx.strokeStyle = "rgba(255, 200, 120, 0.98)";
    drawOutline();
    ctx.setLineDash([]);
    return;
  }

  st.zoneEdit.zones.forEach((zone, idx) => {
    const points = (zone.polygon || []).map(normToCanvas).filter(Boolean);
    if (!points.length) return;
    const selected = idx === st.zoneEdit.selected;
    ctx.lineWidth = selected ? 2.5 : 1.5;
    ctx.strokeStyle = selected ? "#e8a558" : "rgba(104, 176, 171, 0.86)";
    ctx.fillStyle = selected ? "rgba(232, 165, 88, 0.18)" : "rgba(104, 176, 171, 0.12)";
    ctx.beginPath();
    points.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
    if (points.length >= 3) {
      ctx.closePath();
      ctx.fill();
    }
    ctx.stroke();
  });

  selectedPoints().map(normToCanvas).filter(Boolean).forEach((p, i) => {
    ctx.beginPath();
    ctx.arc(p.x, p.y, 5.5, 0, Math.PI * 2);
    ctx.fillStyle = i === st.zoneEdit.dragPoint ? "#f3b46a" : "#e8a558";
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#050709";
    ctx.stroke();
  });
}

function setZoneEditing(active) {
  st.zoneEdit.active = active;
  el.stage?.classList.toggle("zone-editing", active);
  if (el.zoneBar) el.zoneBar.hidden = !active;
  updateZoneControl();
  updateZoneChrome();
  drawZones();
}

function updateZoneChrome() {
  const zones = st.zoneEdit.zones;
  const selected = selectedDraftZone();
  const invalid = zones.some(z => {
    const n = (z.polygon || []).length;
    return n > 0 && n < 3;
  });
  if (el.zoneSave) el.zoneSave.disabled = invalid;
  if (el.zoneDelete) el.zoneDelete.disabled = !selected;
  if (el.zonePrev) el.zonePrev.disabled = zones.length <= 1;
  if (el.zoneNext) el.zoneNext.disabled = zones.length <= 1;
  if (el.zoneName) {
    el.zoneName.disabled = !selected;
    const nextName = selected?.name || "";
    if (document.activeElement !== el.zoneName && el.zoneName.value !== nextName) {
      el.zoneName.value = nextName;
    }
  }
  if (el.zoneCount) {
    const validCount = zones.filter(z => (z.polygon || []).length >= 3).length;
    const total = zones.length;
    el.zoneCount.textContent = total
      ? `${Math.max(0, st.zoneEdit.selected) + 1} of ${total} · ${validCount} saved`
      : "0 areas";
  }
}

function selectZone(idx) {
  const n = st.zoneEdit.zones.length;
  if (!n) {
    st.zoneEdit.selected = -1;
  } else {
    st.zoneEdit.selected = (idx + n) % n;
  }
  st.zoneEdit.dragPoint = null;
  st.zoneEdit.dragPoly = false;
  st.zoneEdit.last = null;
  updateZoneChrome();
  drawZones();
}

function addZoneDraft() {
  const zone = normalizeDraftZone(null, st.zoneEdit.zones.length);
  st.zoneEdit.zones.push(zone);
  selectZone(st.zoneEdit.zones.length - 1);
}

function deleteSelectedZoneDraft() {
  if (!selectedDraftZone()) return;
  st.zoneEdit.zones.splice(st.zoneEdit.selected, 1);
  selectZone(Math.min(st.zoneEdit.selected, st.zoneEdit.zones.length - 1));
}

function startZoneEditor() {
  if (st.source === "all") return;
  player.pause();
  el.liveVideo?.pause();
  st.zoneEdit.zones = activityZones().map(normalizeDraftZone);
  st.zoneEdit.selected = st.zoneEdit.zones.length ? 0 : -1;
  st.zoneEdit.dragPoint = null;
  st.zoneEdit.dragPoly = false;
  st.zoneEdit.last = null;
  setZoneEditing(true);
}

function cancelZoneEditor() {
  st.zoneEdit.zones = [];
  st.zoneEdit.selected = -1;
  st.zoneEdit.dragPoint = null;
  st.zoneEdit.dragPoly = false;
  st.zoneEdit.last = null;
  setZoneEditing(false);
}

async function saveZoneEditor() {
  if (st.source === "all") return;
  if (st.zoneEdit.zones.some(z => {
    const n = (z.polygon || []).length;
    return n > 0 && n < 3;
  })) return;
  const zones = st.zoneEdit.zones
    .filter(z => (z.polygon || []).length >= 3)
    .map((z, idx) => ({
      id: z.id,
      uid: z.uid,
      name: z.name || `Area ${idx + 1}`,
      type: "activity_area",
      enabled: true,
      polygon: z.polygon,
    }));
  const p = new URLSearchParams({ source: st.source });
  const r = await fetch(`/api/video/zones?${p}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ zones }),
  }).catch(() => null);
  if (!r?.ok) return;
  const data = await r.json();
  st.zones = data.zones || [];
  st.zonesSource = st.source;
  cancelZoneEditor();
  st.events = [];
  _eventsRangesClear();
  await load();
}

function resetZoneEditor() {
  st.zoneEdit.zones = activityZones().map(normalizeDraftZone);
  st.zoneEdit.selected = st.zoneEdit.zones.length ? 0 : -1;
  st.zoneEdit.dragPoint = null;
  st.zoneEdit.dragPoly = false;
  st.zoneEdit.last = null;
  updateZoneChrome();
  drawZones();
}

function moveZonePolygon(dx, dy) {
  const pts = selectedPoints();
  if (!pts.length) return;
  const minX = Math.min(...pts.map(p => p.x));
  const maxX = Math.max(...pts.map(p => p.x));
  const minY = Math.min(...pts.map(p => p.y));
  const maxY = Math.max(...pts.map(p => p.y));
  const clampedDx = Math.max(-minX, Math.min(1 - maxX, dx));
  const clampedDy = Math.max(-minY, Math.min(1 - maxY, dy));
  pts.forEach(p => {
    p.x += clampedDx;
    p.y += clampedDy;
  });
}

function pointInPoly(pt, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const a = poly[i], b = poly[j];
    if ((a.y > pt.y) !== (b.y > pt.y)) {
      const xAtY = (b.x - a.x) * (pt.y - a.y) / (b.y - a.y) + a.x;
      if (pt.x <= xAtY) inside = !inside;
    }
  }
  return inside;
}

// ── Auto-refresh ──────────────────────────────────────
setInterval(async () => {
  setStatus(liveTail.active ? "LIVE" : "SYNC");
  const nowTs = Date.now() / 1000;
  // Only advance right edge when viewing recent content — never override
  // manual scroll into history (would corrupt the events window range)
  if (st.window.to > nowTs - 7200) {
    st.window.to = nowTs + LIVE_TIMELINE_FUTURE_PAD_SECONDS;
    setTimelineWindow(st.window.from, st.window.to);
  }
  await load();
  setStatus(liveTail.active ? "LIVE" : "REPLAY");
}, 15000);

window.addEventListener("resize", () => {
  timeline.draw();
  drawZones();
});

// ── Deep links ────────────────────────────────────────
function pushState() {
  const p = new URLSearchParams();
  if (st.source !== "all")    p.set("source", st.source);
  if (liveTail.active) {
    p.set("live", "1");
    const ts = urlTimestamp(liveTailCurrentTs());
    if (liveTail.targetTs != null && ts) p.set("ts", ts);
  }
  else {
    const ts = player.reliableTs;
    const urlTs = urlTimestamp(ts);
    if (urlTs)                p.set("ts", urlTs);
  }
  if (st.cls.size > 0)        p.set("cls",  [...st.cls].join(","));
  if (st.xls.size > 0)        p.set("xcls", [...st.xls].join(","));
  p.set("zone", selectedZoneParam());
  history.replaceState(null, "", `${location.pathname}${p.size ? "?" + p : ""}`);
}

function applyQueryState(p) {
  st.source = p.has("source") ? p.get("source") : "all";
  st.cls.clear();
  st.xls.clear();
  if (p.has("cls"))  p.get("cls").split(",").filter(Boolean).forEach(c => st.cls.add(c));
  if (p.has("xcls")) p.get("xcls").split(",").filter(Boolean).forEach(c => st.xls.add(c));
  st.activeZoneId = null;
  if (p.has("zone")) {
    const z = p.get("zone");
    if (z === ZONE_ALL) {
      st.activeZoneId = ZONE_ALL;
    } else if (z === ZONE_NONE || z === "frame" || z === "whole-frame") {
      st.activeZoneId = null;
    } else {
      const n = parseInt(z, 10);
      st.activeZoneId = Number.isFinite(n) ? n : null;
    }
  }
  const tsv = p.has("ts") ? parseFloat(p.get("ts")) : null;
  return { ts: Number.isFinite(tsv) && tsv > 0 ? tsv : null, live: p.get("live") === "1" };
}

function readState() {
  return applyQueryState(new URLSearchParams(location.search));
}

// ── Boot ──────────────────────────────────────────────
async function init() {
  const r = await fetch("/api/sources", { cache:"no-store" });
  if (r.ok) st.sources = (await r.json()).sources || [];
  await fetchSourceStatus();
  startNotificationPolling();
  if (!V2_SPEEDS[st.speed]) st.speed = 1;
  buildSpeedPills();
  player.setRate(selectedPlaybackRate());

  const { ts: urlTs, live: urlLive } = readState();
  if (urlTs || urlLive) st.initDone = true;
  renderSrcCtrl();

  const now = Date.now() / 1000;
  const anchor = urlTs ?? now;
  st.window.from = anchor - 3 * 3600;
  st.window.to   = anchor + 3 * 3600 + LIVE_TIMELINE_FUTURE_PAD_SECONDS;
  setTimelineWindow(st.window.from, st.window.to);

  if (urlLive && urlTs == null) {
    // Direct live URL without a timestamp: load data in background, immediately enter live.
    load();
    startLiveTail(st.source !== "all" ? st.source : null);
  } else if (urlTs) {
    // Fast path: land on the absolute timestamp while timeline data loads.
    seekToTimestamp(st.source !== "all" ? st.source : null, urlTs, { scroll: false });
    load();
  } else {
    await load();
  }
}

init();
