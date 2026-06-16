// Live wall — the god-view landing. One muted live tile per camera, no boxes,
// no timeline, no detector poll. Click a tile → the full single-camera viewer.
// Reuses the server's native-live proxy (master playlist + iOS LL-strip), so
// iOS gets plain HLS and desktop Chrome gets low-latency, same as the viewer.

const WALL_SYNC = 1;      // ride the live edge — no boxes, so no detector sync
const _hls = [];          // live hls.js instances, for teardown on rebuild
const _pcs = [];          // live WebRTC peer connections, for teardown

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

// WebRTC (WHEP) — instant (sub-second) live. mediamtx serves the raw H.264
// path over WebRTC; signaling is proxied by the app (/video/webrtc/<id>/whep),
// media is direct browser<->mediamtx over ICE.
async function attachWebRTC(video, srcId) {
  const pc = new RTCPeerConnection();
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.ontrack = e => { video.srcObject = e.streams[0]; video.play().catch(() => {}); };
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  const r = await fetch(`/video/webrtc/${encodeURIComponent(srcId)}/whep`, {
    method: "POST",
    headers: { "Content-Type": "application/sdp" },
    body: pc.localDescription.sdp,
  }).catch(() => null);
  if (!r?.ok) { pc.close(); throw new Error("whep " + (r?.status ?? "net")); }
  const answer = await r.text();
  await pc.setRemoteDescription({ type: "answer", sdp: answer });
  return pc;
}

// LL-HLS off mediamtx. mediamtx keeps keyframe-aligned segments pre-buffered,
// so this paints in ~200ms — the instant first layer. Returns the hls.js
// instance (or null for native) so the tile can tear it down after the WebRTC
// upgrade. Also the fallback if WebRTC never connects.
async function attachHls(video, srcId, onOffline) {
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
      return hls;
    }
  }
  if (canNative) {
    video.src = url; video.load(); video.play().catch(() => {});
    return null;
  }
  onOffline();   // no MSE + no native HLS (e.g. cheap Android) — see backlog
  return null;
}

function mkVideo(cls) {
  const v = document.createElement("video");
  v.className = cls;
  v.muted = true; v.autoplay = true; v.playsInline = true;
  v.setAttribute("playsinline", ""); v.setAttribute("muted", "");
  return v;
}

function makeTile(src) {
  const tile = document.createElement("a");
  tile.className = "tile";
  // Click-through to the full viewer for this camera (live, person filter).
  tile.href = `/?source=${encodeURIComponent(src.id)}&live=1&cls=person&zone=none`;

  // Two layers: HLS paints instantly (~200ms, the "cache layer"); WebRTC
  // connects behind it and, on its first frame, swaps in for near-zero latency
  // — then HLS is torn down. HLS also stays as the fallback if WebRTC fails.
  const vHls = mkVideo("tile-hls");
  const vRtc = mkVideo("tile-rtc");

  const bar = document.createElement("div");
  bar.className = "tile-bar";
  bar.innerHTML = `<span class="dot"></span><span class="name"></span>`;
  bar.querySelector(".name").textContent = src.name || src.id;

  tile.append(vHls, vRtc, bar);

  const offline = () => { if (!tile.classList.contains("rtc")) tile.classList.add("offline"); };
  vHls.addEventListener("playing", () => {
    tile.classList.remove("offline"); tile.classList.add("live");   // green once painting
  }, { once: true });

  const hlsP = attachHls(vHls, src.id, offline);   // instant first layer

  attachWebRTC(vRtc, src.id).then((pc) => {
    _pcs.push(pc);
    vRtc.addEventListener("playing", () => {
      // Reveal WebRTC on its next painted frame (rVFC) so the cut lands on a real
      // frame, not a black gap. Tear HLS down a beat later — it's hidden behind
      // the opaque WebRTC by then, so no flicker.
      const reveal = () => {
        tile.classList.add("rtc");
        setTimeout(() => {
          hlsP.then((h) => { try { h?.destroy(); } catch {} });
          try { vHls.pause(); vHls.removeAttribute("src"); vHls.load(); } catch {}
        }, 1000);
      };
      if (vRtc.requestVideoFrameCallback) vRtc.requestVideoFrameCallback(reveal);
      else reveal();
    }, { once: true });
  }).catch(() => { /* WebRTC unavailable → HLS layer stays */ });

  return tile;
}

function teardown() {
  while (_hls.length) { try { _hls.pop().destroy(); } catch {} }
  while (_pcs.length) { try { _pcs.pop().close(); } catch {} }
  const grid = document.getElementById("wall");
  grid.querySelectorAll("video").forEach(v => {
    try { v.pause(); v.srcObject = null; v.removeAttribute("src"); v.load(); } catch {}
  });
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
