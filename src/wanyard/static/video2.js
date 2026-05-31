// ═══════════════════════════════════════════════════════
// V2Player — transparent, segment-aware, async seek
// ═══════════════════════════════════════════════════════
class V2Player {
  #v;
  #segs = [];
  #abort = null;
  #activeSeg = null;
  #lastSeek = null;
  #rate = 1;
  #intendedTs = null;  // display target while async seek/load is in flight
  #listeners = { timeupdate: new Set(), play: new Set(), pause: new Set(), ended: new Set() };

  constructor(videoEl) {
    this.#v = videoEl;
    this.#v.addEventListener("timeupdate", () => this.#emit("timeupdate"));
    this.#v.addEventListener("play",       () => this.#emit("play"));
    this.#v.addEventListener("pause",      () => this.#emit("pause"));
    this.#v.addEventListener("ended",      () => this.#emit("ended"));
  }

  setSegments(segs) {
    this.#segs = [...segs].sort((a, b) => a.start_ts - b.start_ts);
    if (this.#activeSeg) {
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

    try {
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
    return this.#activeSeg ? this.#activeSeg.start_ts + this.#v.currentTime : null;
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

  #applyRate() {
    this.#v.defaultPlaybackRate = this.#rate;
    this.#v.playbackRate = this.#rate;
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

  #waitFor(event, signal) {
    return new Promise((resolve, reject) => {
      if (signal.aborted) { reject(new DOMException("aborted")); return; }
      const onEvent = () => { signal.removeEventListener("abort", onAbort); resolve(); };
      const onAbort = () => { this.#v.removeEventListener(event, onEvent); reject(new DOMException("aborted")); };
      this.#v.addEventListener(event, onEvent, { once: true });
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
    const [start] = this.#clips[this.#idx];
    const landing = await this.#player.seek(start);
    if (!this.#active) return;  // cancelled during seek
    if (!landing) return;
    this.#player.play();
  }

  #watchEnd() {
    if (!this.#active) return;
    const [start, end] = this.#clips[this.#idx] ?? [];
    const ts = this.#player.currentTs;
    if (end != null && ts != null && ts >= end) { this.#advance(); return; }

    if (this.#player.ended && end != null) {
      const next = this.#player.nextSegment();
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
    const clips = events.map(e => [e.abs_ts, e.abs_ts + (e.end_off - e.start_off) + POST]);
    this.#handle = this.#player.playClips(clips, startIdx);
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

class V2Timeline {
  #c; #ctx;
  #segs = []; #evts = []; #srcNames = {};
  #from = 0; #to = 0;
  #head = null;
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

  extendBack(hours) {
    this.#from -= hours * 3600;
    this.draw();
    return this.#from;
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

    const interval = this.#labelInterval();
    const multiDay = span > 20 * 3600;
    let t0 = Math.ceil(this.#from / interval) * interval;
    ctx.font = "400 9px 'IBM Plex Mono',monospace";
    ctx.textBaseline = "alphabetic";
    while (t0 <= this.#to) {
      const x = this.#tsToX(t0);
      if (x >= SRC_W && x <= W) {
        const d = new Date(t0 * 1000);
        const time = d.toLocaleTimeString(undefined, { hour:"2-digit", minute:"2-digit" });
        const label = multiDay
          ? `${d.toLocaleDateString(undefined, { day:"2-digit", month:"short" })} ${time}`
          : time;
        ctx.fillStyle = "rgba(255,255,255,0.08)";
        ctx.fillRect(x, 0, 1, H - LABEL_H);
        ctx.fillStyle = "rgba(166,174,190,0.78)";
        ctx.textAlign = "center";
        ctx.fillText(label, x, H - 4);
      }
      t0 += interval;
    }

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
  #labelInterval() {
    const span = this.#to - this.#from;
    if (span > 20 * 3600) return 4 * 3600;
    if (span > 8 * 3600) return 2 * 3600;
    if (span > 3 * 3600) return 3600;
    if (span > 90 * 60) return 30 * 60;
    return 15 * 60;
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
const ZONE_ALL = "all";
const ZONE_NONE = "none";

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
  status:  $("v2Status"),
  liveBtn: $("v2LiveBtn"),
  stage:   document.querySelector(".v2-stage"),
  ruler:   $("v2Ruler"),
};

// ── Core instances ────────────────────────────────────
const player   = new V2Player(el.video);
const timeline = new V2Timeline(el.tlCanvas);
const mode     = new AppMode(player);

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
  source:   "all",
  cls:      new Set(),   // included classes (amber)
  xls:      new Set(),   // excluded classes (red)
  window:        { from: 0, to: 0 },
  eventsLoaded:  { ranges: [] },  // list of {from,to} intervals, merged on insert
  speed:    parseInt(localStorage.getItem("v2speed") || "1"),
  loop:     true,
  showBoxes:localStorage.getItem("v2boxes") !== "0",
  dets:     {},  // segId → [{ts_offset, boxes, classes}]
  initDone: false,
  classSearchSeq: 0,
  summary: { total: 0, classes: {} },
};
const EVENTS_BUFFER = 3 * 3600;   // load 3h extra on each side of visible window

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
  latestDet: null,
  recentDets: [],   // recent detections buffer (sorted by abs_ts asc)
  window: null,
  wallClockOffset: null,
  targetTs: null,
};

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
  const interval = span > 20 * 3600 ? 3 * 3600 : span > 8 * 3600 ? 2 * 3600 : 3600;
  const width = el.ruler.clientWidth || 1;
  const labelW = timeline.labelWidth;
  let t = Math.ceil(st.window.from / interval) * interval;
  while (t <= st.window.to) {
    const tick = document.createElement("span");
    tick.className = "tick";
    tick.style.left = `${labelW + ((t - st.window.from) / span) * Math.max(1, width - labelW)}px`;
    tick.textContent = new Date(t * 1000).toLocaleTimeString(undefined, { hour:"2-digit", minute:"2-digit" });
    el.ruler.appendChild(tick);
    t += interval;
  }
}

function setTimelineWindow(from, to) {
  st.window.from = from;
  st.window.to = to;
  timeline.setWindow(from, to);
  renderRuler();
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
  liveTail.wallClockOffset = ts - target;
  return true;
}

function liveTailCurrentTs() {
  if (liveTail.wallClockOffset != null) {
    return liveTail.wallClockOffset + (el.liveVideo.currentTime || 0);
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
    const isDvrCatchup = liveTail.wallClockOffset != null && lag > LIVE_EDGE_RATE_RESET_SECONDS;
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

function mergeSegments(segments) {
  if (!segments?.length) return;
  const byId = new Map(st.segments.map(s => [s.id, s]));
  segments.forEach(s => byId.set(s.id, { ...(byId.get(s.id) || {}), ...s }));
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
    scrollTimelineToTs(target.abs_ts);
    seekLiveTail(target.source_id, target.abs_ts).then(ok => { if (!ok) startLiveTail(target.source_id); });
    setStatus("LIVE");
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

function eventSeekTs(evt) {
  const seg = st.segments.find(s => s.id === evt.segment_id);
  const start = seg?.start_ts ?? evt.seg_start_ts ?? evt.abs_ts;
  return Math.max(start, evt.abs_ts - 1);
}

function isEventActive(evt, ts) {
  if (ts == null) return false;
  if (evt.provisional) {
    return liveTail.active && liveTail.srcId === evt.source_id && Math.abs(ts - evt.abs_ts) <= 3;
  }
  if (player.currentSeg?.id !== evt.segment_id) return false;
  const dur = Math.max(1, (evt.end_off ?? 0) - (evt.start_off ?? 0));
  return ts >= evt.abs_ts - 1 && ts <= evt.abs_ts + dur + 1;
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
  btn.addEventListener("click", () => {
    if (evt.provisional) {
      seekLiveTail(evt.source_id, evt.abs_ts).then(ok => { if (!ok) startLiveTail(evt.source_id); });
      scrollTimelineToTs(evt.abs_ts);
      return;
    }
    stopLiveTail();
    mode.seekTo(eventSeekTs(evt), evt.source_id); pushState();
    scrollTimelineToTs(evt.abs_ts);
  });

  const thumb = document.createElement("div");
  thumb.className = "ev-thumb";
  let media;
  if (evt.provisional && !evt.hls_id) {
    media = document.createElement("div");
    media.className = "ev-thumb-live";
    media.textContent = "LIVE";
  } else {
    media = document.createElement("img");
    media.loading = "lazy";
    media.alt = "";
    media.src = evt.hls_id
      ? `/api/video/hls-thumb/${evt.hls_id}`
      : `/api/video/event-thumb/${evt.id}`;
  }
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
  const _loadCheckTo = Math.min(st.window.to, Date.now() / 1000);
  const needsEventsLoad = _loadCheckTo > st.window.from && !_eventsRangesCovers(st.window.from, _loadCheckTo);
  if (needsEventsLoad) timeline.setFetchingRange(evFrom, evTo);

  const [sr, evR, cr, zr] = await Promise.all([
    fetch(`/api/video2/timeline?${p}`, { cache:"no-store" }).then(r=>r.json()).catch(()=>({})),
    needsEventsLoad
      ? fetch(`/api/video/events?since=${Math.floor(evFrom)}&until=${Math.ceil(evTo)}&${p}`, { cache:"no-store" }).then(r=>r.json()).catch(()=>({}))
      : Promise.resolve(null),
    fetch(`/api/video/classes?${p}`, { cache:"no-store" }).then(r=>r.json()).catch(()=>({})),
    fetchZonesForSource(),
  ]);

  st.segments = sr.segments || [];
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
      mode.seekTo(Math.max(latest.start_ts, latest.end_ts - 1), latest.source_id, "backward");
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
      stopLiveTail(false);
      st.source = s.id; st.initDone = false;
      st.events = []; st.zones = []; st.zonesSource = null; st.activeZoneId = null; _eventsRangesClear();
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

// ── Timeline drag-to-scroll ───────────────────────────
let _drag = null, _wasDrag = false;
el.tlCanvas.addEventListener("mousedown", e => {
  if (e.button !== 0) return;
  _drag = { startX: e.clientX, fromSnap: st.window.from, toSnap: st.window.to, moved: false };
  el.tlCanvas.style.cursor = "grabbing";
});
window.addEventListener("mousemove", e => {
  if (!_drag) return;
  const dx = e.clientX - _drag.startX;
  if (Math.abs(dx) > 4) _drag.moved = true;
  if (!_drag.moved) return;
  const rect = el.tlCanvas.getBoundingClientRect();
  const span = _drag.toSnap - _drag.fromSnap;
  const pxPerSec = Math.max(1, rect.width - timeline.labelWidth) / span;
  const shift = -dx / pxPerSec;
  const oldest = st.segments.length
    ? st.segments.reduce((m,s) => Math.min(m, s.start_ts), Infinity) - 1800
    : st.window.from;
  const newest = Date.now() / 1000 + LIVE_TIMELINE_FUTURE_PAD_SECONDS;
  let nf = _drag.fromSnap + shift, nt = _drag.toSnap + shift;
  if (nf < oldest) { nf = oldest; nt = oldest + span; }
  if (nt > newest) { nt = newest; nf = newest - span; }
  if (nf < oldest) nf = oldest;
  st.window.from = nf; st.window.to = nt;
  setTimelineWindow(nf, nt);
});
window.addEventListener("mouseup", e => {
  if (!_drag) return;
  el.tlCanvas.style.cursor = "";
  _wasDrag = _drag.moved;
  if (_drag.moved) {
    clearTimeout(_fetchDebounce);
    _fetchDebounce = setTimeout(() => load(), 400);
  }
  _drag = null;
});

// ── Timeline interactions ─────────────────────────────
let _clickTimer = null;
el.tlCanvas.addEventListener("click", e => {
  if (_wasDrag) { _wasDrag = false; return; }
  const rect = el.tlCanvas.getBoundingClientRect();
  const hit  = timeline.decode(e.clientX - rect.left, e.clientY - rect.top);
  if (!hit) return;
  // Delay single-click action so dblclick can cancel it
  clearTimeout(_clickTimer);
  _clickTimer = setTimeout(async () => {
    if (hit.snapEvent) {
      if (hit.snapEvent.provisional) {
        const ok = await seekLiveTail(hit.snapEvent.source_id, hit.snapEvent.abs_ts);
        if (!ok) startLiveTail(hit.snapEvent.source_id);
        scrollTimelineToTs(hit.snapEvent.abs_ts);
        return;
      }
      stopLiveTail(false);
      mode.seekTo(hit.snapEvent.abs_ts, hit.snapEvent.source_id); pushState();
    } else {
      const nowTs = Date.now() / 1000;
      const srcSegs = st.segments.filter(s => s.source_id === hit.srcId && s.end_ts != null);
      const latestEnd = srcSegs.length ? Math.max(...srcSegs.map(s => s.end_ts)) : 0;
      const openSegs = st.segments.filter(s => s.source_id === hit.srcId && s.end_ts == null);
      const openStart = openSegs.length ? Math.max(...openSegs.map(s => s.start_ts)) : 0;
      const liveGapStart = latestEnd || openStart;
      if (liveGapStart > 0 && liveGapStart > nowTs - LIVE_OPEN_MAX_AGE && hit.ts >= liveGapStart) {
        const targetTs = Math.min(hit.ts, nowTs);
        const ok = await seekLiveTail(hit.srcId, targetTs);
        if (!ok) startLiveTail(hit.srcId);
        return;
      }
      stopLiveTail(false);
      mode.seekTo(hit.ts, hit.srcId); pushState();
    }
    el.empty.style.display = "none";
    el.video.style.display = "block";
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
  return {
    oldest: st.segments.length
      ? st.segments.reduce((m,s) => Math.min(m, s.start_ts), Infinity) - 1800
      : st.window.from,
    newest: Date.now() / 1000 + LIVE_TIMELINE_FUTURE_PAD_SECONDS,
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
el.tlCanvas.addEventListener("mouseleave", () => { clearTimeout(hoverTimer); el.thumb.hidden = true; });

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
    if (evt.provisional) {
      seekLiveTail(evt.source_id, evt.abs_ts).then(ok => { if (!ok) startLiveTail(evt.source_id); });
      scrollTimelineToTs(evt.abs_ts);
      return;
    }
    stopLiveTail(false);
    mode.seekTo(evt.abs_ts, evt.source_id); pushState();
    scrollTimelineToTs(evt.abs_ts);
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
      if (e2.provisional) {
        seekLiveTail(e2.source_id, e2.abs_ts).then(ok => { if (!ok) startLiveTail(e2.source_id); });
        scrollTimelineToTs(e2.abs_ts);
        return;
      }
      stopLiveTail(false);
      mode.seekTo(e2.abs_ts, e2.source_id); pushState();
      scrollTimelineToTs(e2.abs_ts);
    });
  }
}

function navNext() {
  const ts = liveTail.active ? liveTailCurrentTs() : player.reliableTs;
  if (ts == null) return;
  const evts = filteredEvts().filter(e => e.abs_ts > ts + 1).sort((a,b) => a.abs_ts - b.abs_ts);
  const evt  = evts[0];
  if (evt) {
    if (evt.provisional) {
      seekLiveTail(evt.source_id, evt.abs_ts).then(ok => { if (!ok) startLiveTail(evt.source_id); });
      scrollTimelineToTs(evt.abs_ts);
      return;
    }
    stopLiveTail(false);
    mode.seekTo(evt.abs_ts, evt.source_id); pushState();
    scrollTimelineToTs(evt.abs_ts);
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
      if (e2.provisional) {
        seekLiveTail(e2.source_id, e2.abs_ts).then(ok => { if (!ok) startLiveTail(e2.source_id); });
        scrollTimelineToTs(e2.abs_ts);
        return;
      }
      stopLiveTail(false);
      mode.seekTo(e2.abs_ts, e2.source_id); pushState();
      scrollTimelineToTs(e2.abs_ts);
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
  if (ts != null) p.set("ts", String(Math.floor(ts)));
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
  liveTail.wallClockOffset = null;
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

  const hlsUrl = `/video/live/${encodeURIComponent(chosen)}/live.m3u8`;

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
    console.log("HLS attach:", hlsUrl, "hls.js:", canUseHlsJs, "native:", !!canNative, "preferNative:", preferNative);
    if (canUseHlsJs) {
      if (token !== liveTail.token) return;
      if (liveTail.hls) { liveTail.hls.destroy(); liveTail.hls = null; }
      const hlsConfig = {
        lowLatencyMode: false,
        // liveSyncDurationCount / liveMaxLatencyDurationCount intentionally omitted.
        // Explicit liveMaxLatencyDurationCount triggers catchup mode during init
        // (currentTime=0 → apparent latency=60s >> 12s limit → max poll rate).
        // HLS.js defaults handle live sync correctly without this bug.
      };
      if (seekTs != null && liveWindow) hlsConfig.startPosition = liveMediaOffsetForTs(liveWindow, seekTs);
      const hls = new HlsCtor(hlsConfig);
      liveTail.hls = hls;
      hls.loadSource(hlsUrl);
      hls.attachMedia(el.liveVideo);
      hls.on(HlsCtor.Events.MANIFEST_PARSED, () => {
        if (seekTs != null && liveWindow) seekLiveVideoToTs(seekTs, liveWindow);
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
        if (seekTs != null && liveWindow) seekLiveVideoToTs(seekTs, liveWindow);
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
    await pollLiveTail();
    if (token !== liveTail.token) return;
    liveTail.pollTimer = setInterval(pollLiveTail, 1500);
    liveTail.clockTimer = setInterval(updateLiveTailClock, 500);
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
  clearInterval(liveTail.pollTimer);
  clearInterval(liveTail.clockTimer);
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
  liveTail.window = null;
  liveTail.wallClockOffset = null;
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
  const p = new URLSearchParams({ source: liveTail.srcId });
  appendZoneParam(p);
  const r = await fetch(`/api/video/live?${p}`, { cache:"no-store" }).catch(() => null);
  if (!r?.ok) return;
  const data = await r.json();
  mergeSegments(data.segments || []);
  replaceProvisionalEvents(data.events || [], liveTail.srcId);
  mergeClassCounts(data.events || []);
  liveTail.latestDet = (data.detections || []).find(d => d.source_id === liveTail.srcId) ?? liveTail.latestDet;
  liveTail.recentDets = (data.recent_detections || []).filter(d => d.source_id === liveTail.srcId);
  renderClsCtrl();
  timeline.setData(allSegsForSrc(), filteredEvts());
  scheduleNearestEvents(true);
  updateLiveTailClock();
}

function updateLiveTailClock() {
  if (!liveTail.active) return;

  // Recover from paused state (tab hidden, autoplay policy, etc.)
  if (!st.zoneEdit.active && el.liveVideo.paused && !el.liveVideo.ended) {
    el.liveVideo.play().catch(() => {});
  }

  const ts = liveTailCurrentTs();
  updateLivePlaybackRate(ts);
  timeline.setPlayhead(ts);
  setTimestampChip(ts, liveTail.srcId, true);
  setStatus("LIVE");
  drawLiveBoxes();
  drawZones();
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
    mode.seekTo(target, src, "backward"); pushState();
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

function downloadCurrentClip() {
  const ts = liveTail.active ? liveTailCurrentTs() : player.reliableTs;
  const src = liveTail.active ? liveTail.srcId : (player.currentSeg?.source_id ?? (st.source !== "all" ? st.source : null));
  if (!ts || !src) {
    setStatus("NONE");
    return;
  }
  const p = new URLSearchParams({ source: src, ts: String(Math.floor(ts)), before: "30", after: "30" });
  el.download?.classList.add("loading");
  const a = document.createElement("a");
  a.href = `/api/video/clip?${p}`;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => el.download?.classList.remove("loading"), 1200);
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
el.download?.addEventListener("click", downloadCurrentClip);
el.downloadFrame?.addEventListener("click", downloadCurrentFrame);

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
  if (!el.zoneMenu || el.zoneMenu.hidden) return;
  if (el.zonePicker?.contains(e.target)) return;
  closeZoneMenu();
});
document.addEventListener("keydown", (e) => {
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
player.on("ended", () => { mode.handleEnded(st.source !== "all" ? st.source : null); });

player.on("timeupdate", () => {
  const ts = player.currentTs;
  if (ts == null) return;
  timeline.setPlayhead(ts);
  setTimestampChip(ts, player.currentSeg?.source_id ?? null, false);
  drawBoxes(ts);
  drawZones();
  scheduleNearestEvents();
});

// ── Box overlay ───────────────────────────────────────
async function loadDets(segId) {
  if (st.dets[segId] != null) return;
  st.dets[segId] = [];  // mark as loading
  const r = await fetch(`/api/video/detections?segment_id=${segId}`, { cache:"no-store" });
  if (r.ok) st.dets[segId] = (await r.json()).detections || [];
}

function drawBoxes(ts) {
  if (liveTail.active) return;
  const v = el.video;
  const seg = player.currentSeg;
  if (!seg) { drawBoxList(v, []); return; }
  if (!st.dets[seg.id]) { loadDets(seg.id); drawBoxList(v, []); return; }

  const off = ts - seg.start_ts;
  const nearest = (st.dets[seg.id] || []).reduce((best, d) =>
    Math.abs(d.ts_offset - off) < Math.abs((best?.ts_offset ?? Infinity) - off) ? d : best, null);
  if (!nearest || Math.abs(nearest.ts_offset - off) > 1.5) { drawBoxList(v, []); return; }

  drawBoxList(v, _filterBoxes(nearest.boxes));
}

function drawLiveBoxes() {
  // Pick the detection matching the displayed video frame's wall-clock time.
  // HLS.js's playingDate gives the camera-time of the currently displayed
  // frame. Detection arrives at the server ~2-3s before the video player
  // shows that camera frame (due to HLS player buffering).
  const displayDate = liveTail.hls?.playingDate;
  let det = liveTail.latestDet;
  if (displayDate && liveTail.recentDets.length) {
    const target = displayDate.getTime() / 1000;
    let best = null, bestDist = Infinity;
    for (const d of liveTail.recentDets) {
      const dist = Math.abs(d.abs_ts - target);
      if (dist < bestDist) { best = d; bestDist = dist; }
    }
    if (best && bestDist < 1.5) det = best;
    else if (bestDist >= 1.5) det = null;
  }
  drawBoxList(el.liveVideo, _filterBoxes(det?.boxes));
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
  if (!st.showBoxes || !v.videoWidth) return;
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
    if (liveTail.targetTs != null) p.set("ts", String(Math.floor(liveTailCurrentTs())));
  }
  else {
    const ts = player.reliableTs;
    if (ts)                   p.set("ts", Math.floor(ts));
  }
  if (st.cls.size > 0)        p.set("cls",  [...st.cls].join(","));
  if (st.xls.size > 0)        p.set("xcls", [...st.xls].join(","));
  p.set("zone", selectedZoneParam());
  history.replaceState(null, "", `${location.pathname}${p.size ? "?" + p : ""}`);
}

function readState() {
  const p = new URLSearchParams(location.search);
  if (p.has("source")) st.source = p.get("source");
  if (p.has("cls"))  p.get("cls").split(",").filter(Boolean).forEach(c => st.cls.add(c));
  if (p.has("xcls")) p.get("xcls").split(",").filter(Boolean).forEach(c => st.xls.add(c));
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
  return { ts: p.has("ts") ? parseFloat(p.get("ts")) : null, live: p.get("live") === "1" };
}

// ── Boot ──────────────────────────────────────────────
async function init() {
  const r = await fetch("/api/sources", { cache:"no-store" });
  if (r.ok) st.sources = (await r.json()).sources || [];
  await fetchSourceStatus();
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

  if (urlLive) {
    // Direct live URL: load data in background, immediately enter live
    load();
    startLiveTail(st.source !== "all" ? st.source : null, urlTs != null ? { seekTs: urlTs } : {});
  } else if (urlTs) {
    // Fast path: start video immediately, load timeline in background
    const srcParam = st.source !== "all" ? `&source=${encodeURIComponent(st.source)}` : "";
    fetch(`/api/video/segment-at?ts=${urlTs}${srcParam}`, { cache:"no-store" })
      .then(r => r.json())
      .then(({ segment }) => {
        if (!segment) return;
        player.setSegments([segment]);
        el.empty.style.display = "none";
        el.video.style.display = "block";
        mode.seekTo(urlTs, segment.source_id);
      });
    load();
  } else {
    await load();
  }
}

init();
