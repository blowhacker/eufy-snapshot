// Live wall — the god-view landing. One muted live tile per camera, no boxes,
// no timeline, no detector poll. Click a tile → the full single-camera viewer.
// Reuses the server's native-live proxy (master playlist + iOS LL-strip), so
// iOS gets plain HLS and desktop Chrome gets low-latency, same as the viewer.

const NATIVE_DELAY = 3;   // seconds behind the edge for the hls.js path

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

async function nativeLiveUrl(srcId) {
  const r = await fetch(`/api/video/native-live?source=${encodeURIComponent(srcId)}`,
                        { cache: "no-store" }).catch(() => null);
  if (!r?.ok) return null;
  const data = await r.json().catch(() => ({}));
  return data.native?.url || null;
}

async function attachLive(video, srcId, onOffline) {
  const url = await nativeLiveUrl(srcId);
  if (!url) { onOffline(); return; }

  const canNative = video.canPlayType("application/vnd.apple.mpegurl");
  const preferNative = Boolean(canNative && shouldUseNativeHls());

  if (!preferNative) {
    const Hls = await loadHlsJs().catch(() => null);
    if (Hls?.isSupported?.()) {
      const hls = new Hls({
        lowLatencyMode: true,
        liveSyncDuration: NATIVE_DELAY,
        liveMaxLatencyDuration: NATIVE_DELAY + 3,
      });
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

(async () => {
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
})();
