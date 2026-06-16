// Live wall — the god-view landing. One muted live tile per camera, no boxes,
// no timeline, no detector poll. Click a tile → the full single-camera viewer.
// Reuses the server's native-live proxy (master playlist + iOS LL-strip), so
// iOS gets plain HLS and desktop Chrome gets low-latency, same as the viewer.

const WALL_SYNC = 1;      // ride the live edge — no boxes, so no detector sync
const _hls = [];          // live hls.js instances, for teardown on rebuild

// Mirror video2.js shouldUseNativeHls(): Safari + iOS play HLS natively (no MSE).
function shouldUseNativeHls() {
  const ua = navigator.userAgent || "";
  const isiOS = /iPad|iPhone|iPod/.test(ua)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const isSafari = /Safari/i.test(ua)
    && !/Chrome|Chromium|CriOS|FxiOS|Edg|OPR|Android/i.test(ua);
  return isiOS || isSafari;
}

let _hlsPromise = null;
function loadHlsJs() {
  if (window.Hls) return Promise.resolve(window.Hls);
  if (!_hlsPromise) {
    _hlsPromise = new Promise((res, rej) => {
      const s = document.createElement("script");
      s.src = "/hls.min.js";
      s.onload = () => res(window.Hls);
      s.onerror = rej;
      document.head.appendChild(s);
    });
  }
  return _hlsPromise;
}

// Raw camera ingest path off mediamtx — NOT the "-stamped" (BITC) stream the
// viewer uses. The wall shows no boxes/timecode, so it skips the stamper
// re-encode: lower latency, and the camera's native codec (H.264) plays where
// the stamped HEVC can't (cheap Android). Same mediamtx, just the un-suffixed
// path, served through the same native-live proxy (iOS LL-strip still applies).
function rawLiveUrl(srcId) {
  return `/video/native-live/${encodeURIComponent(srcId)}/index.m3u8`;
}

async function attachLive(video, srcId, onOffline) {
  const url = rawLiveUrl(srcId);

  const canNative = video.canPlayType("application/vnd.apple.mpegurl");
  const preferNative = Boolean(canNative && shouldUseNativeHls());

  if (!preferNative) {
    const Hls = await loadHlsJs().catch(() => null);
    if (Hls?.isSupported?.()) {
      const hls = new Hls({
        lowLatencyMode: true,
        liveSyncDuration: WALL_SYNC,
        liveMaxLatencyDuration: WALL_SYNC + 4,
      });
      _hls.push(hls);
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
      hls.on(Hls.Events.ERROR, (_, d) => { if (d.fatal) onOffline(); });
      return;
    }
  }
  if (canNative) {
    video.src = url;
    video.load();
    video.play().catch(() => {});
  } else {
    onOffline();   // no MSE + no native HLS (e.g. cheap Android) — see backlog
  }
}

function makeTile(src) {
  const tile = document.createElement("a");
  tile.className = "tile";
  // Click-through to the full viewer for this camera (live, person filter).
  tile.href = `/?source=${encodeURIComponent(src.id)}&live=1&cls=person&zone=none`;

  const v = document.createElement("video");
  v.muted = true; v.autoplay = true; v.playsInline = true;
  v.setAttribute("playsinline", ""); v.setAttribute("muted", "");

  const bar = document.createElement("div");
  bar.className = "tile-bar";
  bar.innerHTML = `<span class="dot"></span><span class="name"></span>`;
  bar.querySelector(".name").textContent = src.name || src.id;

  tile.append(v, bar);

  const offline = () => { tile.classList.remove("live"); tile.classList.add("offline"); };
  v.addEventListener("playing", () => { tile.classList.remove("offline"); tile.classList.add("live"); });
  v.addEventListener("error", offline);

  attachLive(v, src.id, offline);
  return tile;
}

function teardown() {
  while (_hls.length) { try { _hls.pop().destroy(); } catch {} }
  const grid = document.getElementById("wall");
  grid.querySelectorAll("video").forEach(v => { try { v.pause(); v.removeAttribute("src"); v.load(); } catch {} });
  grid.innerHTML = "";
}

async function build() {
  teardown();
  const grid = document.getElementById("wall");
  const sub = document.getElementById("wallSub");
  const r = await fetch("/api/sources", { cache: "no-store" }).catch(() => null);
  const sources = r?.ok ? ((await r.json()).sources || []) : [];

  if (!sources.length) {
    grid.innerHTML = `<div class="empty">No cameras yet. <a href="/settings">Add one →</a></div>`;
    return;
  }
  sub.textContent = `${sources.length} camera${sources.length > 1 ? "s" : ""}`;
  sources.forEach(s => grid.append(makeTile(s)));
}

build();

// iOS restores this page from the back-forward cache (bfcache) without re-running
// the script, and tears down media on navigate-away → tiles come back blank.
// Rebuild from scratch (fresh attach at the current live edge) on bfcache restore.
window.addEventListener("pageshow", e => { if (e.persisted) build(); });
