const PAGE_SIZE = 24;
const RECENT_REFRESH_MS = 4000;

function filtersFromUrl() {
  const params = new URLSearchParams(location.search);
  return {
    camera: params.get("camera") || "all",
    view: params.get("view") === "feed" ? "feed" : "grid",
    classes: new Set(
      (params.get("classes") || "")
        .split(",")
        .map(value => value.trim())
        .filter(Boolean)
    ),
    zones: new Set(
      (params.get("zones") || "")
        .split(",")
        .map(value => value.trim())
        .filter(Boolean)
    ),
  };
}

const dom = {
  main: document.getElementById("dwMain"),
  cameras: document.getElementById("dwCameras"),
  areas: document.getElementById("dwAreas"),
  areasFilter: document.getElementById("dwAreasFilter"),
  tags: document.getElementById("dwTags"),
  summary: document.getElementById("dwSummary"),
  viewToggle: document.getElementById("dwViewToggle"),
  refresh: document.getElementById("dwRefresh"),
  emptyTemplate: document.getElementById("dwEmptyTemplate"),
};

const initialFilters = filtersFromUrl();
const state = {
  camera: initialFilters.camera,
  view: initialFilters.view,
  classes: initialFilters.classes,
  zones: initialFilters.zones,
  classCounts: {},
  availableZones: [],
  sources: [],
  cameras: new Map(),
  request: 0,
  loading: false,
  recentLoading: false,
  recentTimer: null,
};

const canHoverPreview = window.matchMedia(
  "(hover: hover) and (pointer: fine)"
);
let pendingPreview = null;
let activePreview = null;
let feedObserver = null;
const feedVisibility = new Map();
let feedActivationFrame = null;
let feedKeyboardCard = null;
const liveWindowCache = new Map();
const previewTracker = window.DetectionPreviewTrack;

const moreObserver = "IntersectionObserver" in window
  ? new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const button = entry.target.querySelector("button");
        if (button && !button.disabled) button.click();
      }
    }, { rootMargin: "320px 0px" })
  : null;

function previewCrop(
  box,
  frameWidth,
  frameHeight,
  aspect = 4 / 3,
  fixedSize = null
) {
  const x1 = Math.max(0, Math.min(1, Number(box.x1))) * frameWidth;
  const y1 = Math.max(0, Math.min(1, Number(box.y1))) * frameHeight;
  const x2 = Math.max(0, Math.min(1, Number(box.x2))) * frameWidth;
  const y2 = Math.max(0, Math.min(1, Number(box.y2))) * frameHeight;
  if (![x1, y1, x2, y2].every(Number.isFinite) || x2 <= x1 || y2 <= y1) {
    return null;
  }
  const boxWidth = x2 - x1;
  const boxHeight = y2 - y1;
  const centerX = (x1 + x2) / 2;
  const centerY = (y1 + y2) / 2;
  let cropWidth;
  let cropHeight;
  if (fixedSize) {
    cropWidth = Number(fixedSize.width) * frameWidth;
    cropHeight = Number(fixedSize.height) * frameHeight;
  } else {
    const padding = Math.max(24, Math.max(boxWidth, boxHeight) * .45);
    cropWidth = boxWidth + padding * 2;
    cropHeight = boxHeight + padding * 2;
    if (cropWidth / cropHeight < aspect) cropWidth = cropHeight * aspect;
    else cropHeight = cropWidth / aspect;
    cropWidth = Math.min(frameWidth, Math.max(96, cropWidth));
    cropHeight = Math.min(frameHeight, Math.max(72, cropHeight));
    if (cropWidth / cropHeight < aspect) {
      cropWidth = Math.min(frameWidth, cropHeight * aspect);
    } else {
      cropHeight = Math.min(frameHeight, cropWidth / aspect);
    }
  }
  const width = Math.min(frameWidth, Math.max(2, cropWidth));
  const height = Math.min(frameHeight, Math.max(2, cropHeight));
  return {
    x: Math.max(0, Math.min(frameWidth - width, centerX - width / 2)),
    y: Math.max(0, Math.min(frameHeight - height, centerY - height / 2)),
    width,
    height,
  };
}

function layoutActivePreview() {
  if (!activePreview) return;
  const item = activePreview;
  const { video, host, preview } = item;
  const frameWidth = video.videoWidth;
  const frameHeight = video.videoHeight;
  const hostWidth = host.clientWidth;
  const hostHeight = host.clientHeight;
  if (!frameWidth || !frameHeight || !hostWidth || !hostHeight) return;
  if (!item.cropSize) {
    const initialCrop = previewCrop(
      preview.box,
      frameWidth,
      frameHeight
    );
    if (!initialCrop) return;
    item.cropSize = {
      width: initialCrop.width / frameWidth,
      height: initialCrop.height / frameHeight,
    };
  }
  const crop = previewCrop(
    item.panBox || preview.box,
    frameWidth,
    frameHeight,
    4 / 3,
    item.cropSize
  );
  if (!crop) return;
  const scale = Math.max(hostWidth / crop.width, hostHeight / crop.height);
  video.style.width = `${frameWidth * scale}px`;
  video.style.height = `${frameHeight * scale}px`;
  const left = (
    -crop.x * scale + (hostWidth - crop.width * scale) / 2
  );
  const top = (
    -crop.y * scale + (hostHeight - crop.height * scale) / 2
  );
  video.style.left = "0";
  video.style.top = "0";
  video.style.transform = `translate3d(${left}px, ${top}px, 0)`;
}

function previewAbsoluteTime(item, mediaTime = item.video.currentTime) {
  const startTs = Number(item.timelineStartTs);
  const start = Number(item.start);
  const current = Number(mediaTime);
  if (![startTs, start, current].every(Number.isFinite)) return null;
  return startTs + current - start;
}

function updatePreviewPan(item, mediaTime) {
  if (activePreview !== item || !item.track) return;
  const ts = previewAbsoluteTime(item, mediaTime);
  const box = previewTracker?.sampleTrack(item.track, ts);
  if (!box) return;
  const target = {
    x: (Number(box.x1) + Number(box.x2)) / 2,
    y: (Number(box.y1) + Number(box.y2)) / 2,
  };
  const currentMediaTime = Number(mediaTime);
  if (item.panMediaTime == null) {
    // The stored event box can come from later in the episode, while previews
    // include a second of pre-roll. Start at the track's box for the frame
    // actually being shown; easing from the later event box makes fast movers
    // pan backwards briefly before reversing into their true direction.
    item.panCenter = target;
    item.panMediaTime = currentMediaTime;
  } else {
    item.panCenter = previewTracker.dampCenter(
      item.panCenter,
      target,
      currentMediaTime - item.panMediaTime
    );
    item.panMediaTime = currentMediaTime;
  }
  const center = item.panCenter || target;
  const halfWidth = (Number(box.x2) - Number(box.x1)) / 2;
  const halfHeight = (Number(box.y2) - Number(box.y1)) / 2;
  item.panBox = {
    ...box,
    x1: center.x - halfWidth,
    y1: center.y - halfHeight,
    x2: center.x + halfWidth,
    y2: center.y + halfHeight,
  };
  layoutActivePreview();
}

function schedulePreviewPan(item) {
  if (activePreview !== item || item.panFrame != null) return;
  if ("requestVideoFrameCallback" in item.video) {
    item.panFrameKind = "video";
    item.panFrame = item.video.requestVideoFrameCallback((_, metadata) => {
      item.panFrame = null;
      updatePreviewPan(item, metadata.mediaTime);
      schedulePreviewPan(item);
    });
  } else {
    item.panFrameKind = "animation";
    item.panFrame = requestAnimationFrame(() => {
      item.panFrame = null;
      updatePreviewPan(item, item.video.currentTime);
      schedulePreviewPan(item);
    });
  }
}

async function loadPreviewTrack(item, refreshesLeft = 0) {
  const preview = item.trackingPreview;
  if (
    activePreview !== item
    || !previewTracker
    || !preview?.source_id
    || !preview?.class
    || !Number.isFinite(Number(preview.start_ts))
    || !Number.isFinite(Number(preview.end_ts))
  ) {
    settlePreviewTrack(item);
    return;
  }
  item.trackAbort?.abort();
  const abort = new AbortController();
  item.trackAbort = abort;
  const params = new URLSearchParams({
    source: preview.source_id,
    since: String(Math.floor(Number(preview.start_ts) - 1)),
    until: String(Math.ceil(Number(preview.end_ts) + 1)),
  });
  const data = await fetch(`/api/video/overlays?${params}`, {
    cache: "no-store",
    signal: abort.signal,
  })
    .then(response => response.ok ? response.json() : null)
    .catch(() => null);
  if (activePreview !== item || abort.signal.aborted) return;
  const tracks = previewTracker.buildTracks(
    data?.detections || [],
    preview.class
  );
  item.track = previewTracker.selectTrack(
    tracks,
    Number(preview.event_ts),
    preview.box
  );
  updatePreviewPan(
    item,
    item.videoPlaying ? item.video.currentTime : item.start
  );
  settlePreviewTrack(item);
  if (refreshesLeft > 0) {
    item.trackRefreshTimer = setTimeout(
      () => loadPreviewTrack(item, refreshesLeft - 1),
      1000
    );
  }
}

function revealPreview(item) {
  if (
    activePreview !== item
    || !item.videoPlaying
    || !item.trackReady
  ) return;
  clearTimeout(item.trackRevealTimer);
  item.trackRevealTimer = null;
  item.card.classList.remove("preview-loading");
  if (!item.card.classList.contains("preview-playing")) {
    layoutActivePreview();
    // Commit the initial tracked transform before enabling its transition.
    // Otherwise the video fades in while flying from the event thumbnail's
    // later box to the actual pre-roll position.
    void item.video.offsetWidth;
    item.card.classList.add("preview-playing");
  }
}

function settlePreviewTrack(item) {
  if (activePreview !== item) return;
  item.trackReady = true;
  revealPreview(item);
}

function cancelPendingPreview(card = null) {
  if (!pendingPreview || (card && pendingPreview.card !== card)) return;
  const pendingCard = pendingPreview.card;
  clearTimeout(pendingPreview.timer);
  pendingPreview = null;
  pendingCard.classList.remove("preview-loading");
}

// Keep this aligned with the main viewer and live wall: Safari/iOS have a
// reliable native HLS implementation, while desktop Chromium's advertised
// native path can aggressively reload live playlists.
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
  if (!window.__dwHlsPromise) {
    window.__dwHlsPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/hls.min.js";
      script.onload = () => resolve(window.Hls);
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }
  return window.__dwHlsPromise;
}

async function fetchLiveWindow(sourceId) {
  const cached = liveWindowCache.get(sourceId);
  if (cached && Date.now() - cached.at < 3000) return cached.promise;
  const params = new URLSearchParams({ source: sourceId });
  const promise = fetch(`/api/video/live-window?${params}`, {
    cache: "no-store",
  })
    .then(response => response.ok ? response.json() : null)
    .then(data => data?.window || null)
    .catch(() => null);
  liveWindowCache.set(sourceId, { at: Date.now(), promise });
  return promise;
}

function liveWindowContains(window, ts) {
  return Boolean(
    window
    && Number(ts) >= Number(window.start_ts) - 1
    && Number(ts) <= Number(window.end_ts) + 1
  );
}

function liveSeekTarget(video, window, ts) {
  const offset = Math.max(
    0,
    Math.min(
      Math.max(0, Number(window.end_ts) - Number(window.start_ts) - .25),
      Number(ts) - Number(window.start_ts)
    )
  );
  const ranges = video.seekable;
  if (!ranges?.length) return offset;
  const start = ranges.start(0);
  const end = ranges.end(ranges.length - 1);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
    return offset;
  }
  return Math.max(start, Math.min(Math.max(start, end - .25), start + offset));
}

function stopPreview(card = null, failed = false) {
  cancelPendingPreview(card);
  if (!activePreview || (card && activePreview.card !== card)) return;
  const {
    card: activeCard,
    video,
    resizeObserver,
    hls,
    panFrame,
    panFrameKind,
    trackAbort,
    trackRefreshTimer,
    loopTimer,
    trackRevealTimer,
    hlsReadyTimer,
  } = activePreview;
  activePreview = null;
  clearTimeout(trackRefreshTimer);
  clearTimeout(loopTimer);
  clearTimeout(trackRevealTimer);
  clearTimeout(hlsReadyTimer);
  trackAbort?.abort();
  if (panFrame != null) {
    if (panFrameKind === "video") video.cancelVideoFrameCallback?.(panFrame);
    else cancelAnimationFrame(panFrame);
  }
  resizeObserver?.disconnect();
  hls?.destroy();
  activeCard.classList.remove(
    "preview-loading",
    "preview-playing",
    "preview-looping",
    "preview-seeking"
  );
  if (failed) activeCard.dataset.previewFailed = "1";
  try {
    video.pause();
    video.removeAttribute("src");
    video.load();
  } catch {}
  video.remove();
}

function restartPreview(item) {
  if (activePreview !== item || item.looping) return;
  item.looping = true;
  item.video.pause();
  item.card.classList.add("preview-looping");
  item.loopTimer = setTimeout(() => {
    item.loopTimer = null;
    if (activePreview !== item) return;
    item.card.classList.add("preview-seeking");
    item.panMediaTime = null;
    const reveal = () => {
      if (activePreview !== item) return;
      updatePreviewPan(item, item.video.currentTime);
      item.video.play().catch(() => {});
      requestAnimationFrame(() => {
        if (activePreview !== item) return;
        item.card.classList.remove("preview-looping", "preview-seeking");
        item.looping = false;
      });
    };
    item.video.addEventListener("seeked", reveal, { once: true });
    try {
      item.video.currentTime = item.start;
    } catch {
      stopPreview(item.card, true);
    }
  }, 110);
}

function startMp4Preview(item, preview) {
  const { card, video } = item;
  item.mode = "mp4";
  item.preview = preview;
  item.start = Math.max(0, Number(preview.start) || 0);
  item.end = Math.max(0, Number(preview.end) || 0);
  const requestedStart = item.start;
  video.addEventListener("loadedmetadata", () => {
    if (
      activePreview !== item
      || item.mode !== "mp4"
      || !Number.isFinite(video.duration)
    ) return;
    item.start = Math.min(item.start, Math.max(0, video.duration - .1));
    item.end = Math.min(
      video.duration,
      Math.max(item.start + .25, item.end)
    );
    item.timelineStartTs = Number(preview.start_ts)
      + (item.start - requestedStart);
    layoutActivePreview();
    const play = () => {
      if (activePreview === item && item.mode === "mp4") {
        video.play().catch(() => stopPreview(card, true));
      }
    };
    if (item.start > .01) {
      video.addEventListener("seeked", play, { once: true });
      video.currentTime = item.start;
    } else {
      play();
    }
  }, { once: true });
  video.src = preview.url;
  video.load();
}

async function fallbackToRecordedPreview(item) {
  if (
    activePreview !== item
    || item.fallbackStarted
    || !item.hlsPreview
  ) return;
  item.fallbackStarted = true;
  item.mode = "resolving";
  clearTimeout(item.hlsReadyTimer);
  item.hlsReadyTimer = null;
  item.hls?.destroy();
  item.hls = null;

  const preview = item.hlsPreview;
  const params = new URLSearchParams({
    source: preview.source_id,
    ts: String(preview.event_ts ?? preview.start_ts),
  });
  const resolved = await fetch(`/api/video/resolve?${params}`, {
    cache: "no-store",
  })
    .then(response => response.ok ? response.json() : null)
    .catch(() => null);
  if (activePreview !== item) return;

  const mediaEpoch = Number(resolved?.media_epoch);
  const duration = Number(resolved?.duration);
  if (
    (resolved?.storage_provider !== "mp4" && resolved?.provider !== "mp4")
    || !resolved?.url
    || !Number.isFinite(mediaEpoch)
  ) {
    stopPreview(item.card);
    return;
  }
  const maxPosition = Number.isFinite(duration)
    ? Math.max(0, duration - .1)
    : Infinity;
  const eventTs = Number(preview.event_ts ?? preview.start_ts);
  const desiredStartTs = eventTs - 1;
  const desiredEndTs = Math.min(
    desiredStartTs + 8,
    Math.max(desiredStartTs + 3, Number(preview.end_ts))
  );
  const start = Math.max(
    0,
    Math.min(maxPosition, desiredStartTs - mediaEpoch)
  );
  const end = Math.max(
    start + .25,
    Math.min(maxPosition, desiredEndTs - mediaEpoch)
  );
  startMp4Preview(item, {
    url: resolved.url,
    source_id: preview.source_id,
    class: preview.class,
    event_ts: eventTs,
    start_ts: desiredStartTs,
    end_ts: desiredEndTs,
    start,
    end,
    box: preview.box,
  });
}

async function startHlsPreview(item) {
  const { card, preview, video } = item;
  const window = await fetchLiveWindow(preview.source_id);
  if (activePreview !== item) return;
  if (
    !liveWindowContains(window, preview.start_ts)
    || !liveWindowContains(window, preview.end_ts)
  ) {
    fallbackToRecordedPreview(item);
    return;
  }

  const startTs = Math.max(Number(window.start_ts), Number(preview.start_ts));
  const endTs = Math.min(Number(window.end_ts), Number(preview.end_ts));
  if (!Number.isFinite(startTs) || !Number.isFinite(endTs) || endTs - startTs < .25) {
    stopPreview(card, true);
    return;
  }

  let readyAttempts = 0;
  const ready = () => {
    if (activePreview !== item || item.mode !== "hls") return;
    if (!video.seekable?.length) {
      // Native HLS can fire loadedmetadata before its DVR range exists.
      // Seeking at that point is silently clamped to zero, leaving the loop
      // boundary on a different timeline and making the preview appear stuck.
      if (readyAttempts++ < 30) {
        item.hlsReadyTimer = setTimeout(ready, 50);
      } else {
        fallbackToRecordedPreview(item);
      }
      return;
    }
    clearTimeout(item.hlsReadyTimer);
    item.hlsReadyTimer = null;
    item.start = liveSeekTarget(video, window, startTs);
    item.end = item.start + (endTs - startTs);
    item.timelineStartTs = startTs;
    if (video.seekable?.length) {
      const seekableEnd = video.seekable.end(video.seekable.length - 1);
      item.end = Math.min(item.end, seekableEnd);
    }
    if (item.end - item.start < .25) {
      fallbackToRecordedPreview(item);
      return;
    }
    layoutActivePreview();
    try {
      video.currentTime = item.start;
      video.play().catch(() => stopPreview(card, true));
    } catch {
      stopPreview(card, true);
    }
  };
  video.addEventListener("loadedmetadata", ready, { once: true });

  const canPlayNative = Boolean(
    video.canPlayType("application/vnd.apple.mpegurl")
  );
  if (canPlayNative && shouldUseNativeHls()) {
    video.src = preview.url;
    video.load();
    return;
  }

  const HlsCtor = await loadHlsJs().catch(() => null);
  if (activePreview !== item) return;
  if (HlsCtor?.isSupported?.()) {
    const hls = new HlsCtor({
      lowLatencyMode: false,
      startPosition: Math.max(0, startTs - Number(window.start_ts)),
      maxBufferLength: 12,
      backBufferLength: 8,
    });
    item.hls = hls;
    hls.on(HlsCtor.Events.ERROR, (_, data) => {
      if (activePreview === item && data.fatal) {
        fallbackToRecordedPreview(item);
      }
    });
    hls.loadSource(preview.url);
    hls.attachMedia(video);
    return;
  }

  if (canPlayNative) {
    video.src = preview.url;
    video.load();
    return;
  }
  fallbackToRecordedPreview(item);
}

function startPreview(card, preview, { force = false } = {}) {
  pendingPreview = null;
  if (
    (!force && !canHoverPreview.matches)
    || !card.isConnected
    || card.dataset.previewFailed
  ) {
    card.classList.remove("preview-loading");
    return;
  }
  stopPreview();
  const host = card.querySelector(".dw-thumb");
  if (!host) {
    card.classList.remove("preview-loading");
    return;
  }
  const video = document.createElement("video");
  video.className = "dw-preview-video";
  video.muted = true;
  video.playsInline = true;
  video.preload = "metadata";
  video.setAttribute("muted", "");
  video.setAttribute("playsinline", "");
  const item = {
    card,
    host,
    video,
    preview,
    start: Math.max(0, Number(preview.start) || 0),
    end: Math.max(0, Number(preview.end) || 0),
    resizeObserver: null,
    hls: null,
    hlsPreview: preview.kind === "hls" ? preview : null,
    fallbackStarted: false,
    mode: preview.kind === "hls" ? "hls" : "mp4",
    trackingPreview: preview,
    timelineStartTs: Number(preview.start_ts),
    track: null,
    trackAbort: null,
    trackRefreshTimer: null,
    loopTimer: null,
    looping: false,
    videoPlaying: false,
    trackReady: false,
    trackRevealTimer: null,
    hlsReadyTimer: null,
    panBox: preview.box,
    panCenter: {
      x: (Number(preview.box.x1) + Number(preview.box.x2)) / 2,
      y: (Number(preview.box.y1) + Number(preview.box.y2)) / 2,
    },
    panMediaTime: null,
    cropSize: null,
    panFrame: null,
    panFrameKind: null,
  };
  activePreview = item;

  video.addEventListener("playing", () => {
    if (activePreview !== item) return;
    item.videoPlaying = true;
    revealPreview(item);
  });
  video.addEventListener("waiting", () => {
    if (activePreview === item) card.classList.add("preview-loading");
  });
  video.addEventListener("timeupdate", () => {
    if (
      activePreview === item
      && video.currentTime >= item.end - .05
    ) {
      restartPreview(item);
    }
  });
  video.addEventListener("ended", () => {
    restartPreview(item);
  });
  video.addEventListener("error", () => {
    if (activePreview !== item) return;
    if (item.mode === "hls") fallbackToRecordedPreview(item);
    else if (item.mode === "mp4") stopPreview(card, true);
  });
  if ("ResizeObserver" in window) {
    item.resizeObserver = new ResizeObserver(layoutActivePreview);
    item.resizeObserver.observe(host);
  }
  host.append(video);
  schedulePreviewPan(item);
  item.trackRevealTimer = setTimeout(
    () => settlePreviewTrack(item),
    450
  );
  loadPreviewTrack(item, preview.kind === "hls" ? 2 : 0);
  if (preview.kind === "hls") startHlsPreview(item);
  else startMp4Preview(item, preview);
}

function queuePreview(card, preview) {
  if (
    state.view === "feed"
    || !preview
    || !canHoverPreview.matches
    || card.dataset.previewFailed
  ) return;
  if (activePreview?.card === card) return;
  cancelPendingPreview();
  card.classList.add("preview-loading");
  pendingPreview = {
    card,
    timer: setTimeout(() => startPreview(card, preview), 180),
  };
}

function stopFeedObserver() {
  feedObserver?.disconnect();
  feedObserver = null;
  feedVisibility.clear();
  feedKeyboardCard = null;
  if (feedActivationFrame != null) {
    cancelAnimationFrame(feedActivationFrame);
    feedActivationFrame = null;
  }
  document.querySelector(".dw-card.feed-current")
    ?.classList.remove("feed-current");
}

function activateVisibleFeedCard() {
  feedActivationFrame = null;
  if (state.view !== "feed" || document.hidden) return;
  let bestCard = null;
  let bestRatio = 0;
  for (const [card, ratio] of feedVisibility) {
    if (card.isConnected && ratio > bestRatio) {
      bestCard = card;
      bestRatio = ratio;
    }
  }
  if (!bestCard || bestRatio < .55) {
    stopPreview();
    return;
  }
  const current = document.querySelector(".dw-card.feed-current");
  if (current !== bestCard) {
    current?.classList.remove("feed-current");
    bestCard.classList.add("feed-current");
  }
  feedKeyboardCard = bestCard;
  if (activePreview?.card === bestCard || pendingPreview?.card === bestCard) {
    return;
  }
  stopPreview();
  if (bestCard._preview) {
    bestCard.classList.add("preview-loading");
    startPreview(bestCard, bestCard._preview, { force: true });
  }
}

function scheduleFeedActivation() {
  if (feedActivationFrame != null) return;
  feedActivationFrame = requestAnimationFrame(activateVisibleFeedCard);
}

function startFeedObserver() {
  stopFeedObserver();
  if (state.view !== "feed" || !("IntersectionObserver" in window)) return;
  feedObserver = new IntersectionObserver(entries => {
    for (const entry of entries) {
      feedVisibility.set(entry.target, entry.intersectionRatio);
    }
    scheduleFeedActivation();
  }, {
    root: dom.main,
    threshold: [0, .25, .55, .75, 1],
  });
  for (const card of dom.main.querySelectorAll(".dw-card")) {
    feedObserver.observe(card);
  }
}

function moveFeed(direction) {
  if (state.view !== "feed") return false;
  const cards = [...dom.main.querySelectorAll(".dw-card")];
  if (!cards.length) return false;
  const current = feedKeyboardCard?.isConnected
    ? feedKeyboardCard
    : document.querySelector(".dw-card.feed-current");
  const currentIndex = Math.max(0, cards.indexOf(current));
  const targetIndex = Math.max(
    0,
    Math.min(cards.length - 1, currentIndex + direction)
  );
  if (targetIndex === currentIndex) return true;
  const target = cards[targetIndex];
  feedKeyboardCard = target;
  const mainRect = dom.main.getBoundingClientRect();
  const targetRect = target.getBoundingClientRect();
  const top = dom.main.scrollTop + targetRect.top - mainRect.top;
  const reduceMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)"
  ).matches;
  dom.main.scrollTo({
    top,
    behavior: reduceMotion ? "auto" : "smooth",
  });
  return true;
}

function updateViewMode() {
  const feed = state.view === "feed";
  document.body.classList.toggle("dw-feed-mode", feed);
  dom.viewToggle.classList.toggle("active", feed);
  dom.viewToggle.setAttribute("aria-pressed", String(feed));
  dom.viewToggle.title = feed ? "Use grid view" : "Use feed view";
  dom.viewToggle.setAttribute(
    "aria-label",
    feed ? "Use grid view" : "Use feed view"
  );
}

function setView(view, { updateHistory = true } = {}) {
  const next = view === "feed" ? "feed" : "grid";
  if (state.view === next) return;
  stopPreview();
  stopFeedObserver();
  state.view = next;
  updateViewMode();
  if (updateHistory) syncUrl();
  renderCameras();
  if (next === "feed") dom.main.scrollTop = 0;
}

function classLabel(value) {
  const text = String(value || "motion").replaceAll("_", " ");
  return text.slice(0, 1).toUpperCase() + text.slice(1);
}

function plural(value, noun) {
  return `${value} ${noun}${value === 1 ? "" : "s"}`;
}

function eventTime(ts) {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(Number(ts) * 1000));
}

function eventTitle(ts) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "full",
    timeStyle: "long",
  }).format(new Date(Number(ts) * 1000));
}

function eventDuration(event) {
  const seconds = Math.max(1, Math.round(
    Number(event.end_off || 0) - Number(event.start_off || 0)
  ));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
}

function selectedClasses() {
  return [...state.classes].sort();
}

function selectedZones() {
  return [...state.zones].sort();
}

function syncUrl({ replace = false } = {}) {
  const url = new URL(location.href);
  if (state.camera && state.camera !== "all") {
    url.searchParams.set("camera", state.camera);
  } else {
    url.searchParams.delete("camera");
  }
  const classes = selectedClasses();
  if (classes.length) url.searchParams.set("classes", classes.join(","));
  else url.searchParams.delete("classes");
  const zones = selectedZones();
  if (zones.length) url.searchParams.set("zones", zones.join(","));
  else url.searchParams.delete("zones");
  if (state.view === "feed") url.searchParams.set("view", "feed");
  else url.searchParams.delete("view");
  history[replace ? "replaceState" : "pushState"](
    { detectionWall: true },
    "",
    url
  );
}

function restoreFiltersFromUrl() {
  const filters = filtersFromUrl();
  state.camera = filters.camera;
  state.view = filters.view;
  state.classes = filters.classes;
  state.zones = filters.zones;
  updateViewMode();
}

function apiUrl({
  source = state.camera,
  before = null,
  limit = PAGE_SIZE,
  counts = true,
} = {}) {
  const params = new URLSearchParams({ limit: String(limit) });
  const classes = selectedClasses();
  if (classes.length) params.set("classes", classes.join(","));
  const zones = selectedZones();
  if (zones.length) params.set("zones", zones.join(","));
  params.set("source", source || "all");
  if (!counts) params.set("counts", "0");
  if (Number.isFinite(before)) params.set("before", String(before));
  return `/api/detections/wall?${params}`;
}

async function fetchWall(options = {}) {
  const response = await fetch(apiUrl(options), { cache: "no-store" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function makeTag(value, label, count, active) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `dw-tag${active ? " active" : ""}`;
  button.setAttribute("aria-pressed", String(active));
  button.dataset.class = value;

  const text = document.createElement("span");
  text.textContent = label;
  const number = document.createElement("span");
  number.className = "dw-tag-count";
  number.textContent = String(count);
  button.append(text, number);

  button.addEventListener("click", () => {
    if (!value) {
      if (state.classes.size === 0) return;
      state.classes.clear();
    } else if (state.classes.has(value)) {
      state.classes.delete(value);
    } else {
      state.classes.add(value);
    }
    syncUrl();
    loadInitial();
  });
  return button;
}

function makeCameraTag(value, label) {
  const button = document.createElement("button");
  button.type = "button";
  const active = state.camera === value;
  button.className = `dw-tag${active ? " active" : ""}`;
  button.setAttribute("aria-pressed", String(active));
  button.textContent = label;
  button.addEventListener("click", () => {
    if (state.camera === value) return;
    state.camera = value;
    syncUrl();
    loadInitial();
  });
  return button;
}

function renderCameraTags() {
  dom.cameras.innerHTML = "";
  dom.cameras.append(makeCameraTag("all", "All"));
  for (const source of state.sources) {
    dom.cameras.append(makeCameraTag(source.id, source.name || source.id));
  }
}

function selectedZonesForSource(sourceId) {
  return new Set(
    state.availableZones
      .filter(zone => zone.source_id === sourceId && state.zones.has(zone.uid))
      .map(zone => zone.uid)
  );
}

function makeAreaTag(sourceId, value, label) {
  const button = document.createElement("button");
  button.type = "button";
  const sourceZones = selectedZonesForSource(sourceId);
  const active = value ? sourceZones.has(value) : sourceZones.size === 0;
  button.className = `dw-tag${active ? " active" : ""}`;
  button.setAttribute("aria-pressed", String(active));
  button.textContent = label;
  button.addEventListener("click", () => {
    if (!value) {
      if (sourceZones.size === 0) return;
      for (const uid of sourceZones) state.zones.delete(uid);
    } else if (state.zones.has(value)) {
      state.zones.delete(value);
    } else {
      state.zones.add(value);
    }
    syncUrl();
    loadInitial();
  });
  return button;
}

function renderAreaTags() {
  dom.areas.innerHTML = "";
  const visibleSources = state.camera === "all"
    ? state.sources
    : state.sources.filter(source => source.id === state.camera);
  const groups = visibleSources
    .map(source => ({
      source,
      zones: state.availableZones.filter(zone => zone.source_id === source.id),
    }))
    .filter(group => group.zones.length);
  dom.areasFilter.hidden = groups.length === 0;
  if (dom.areasFilter.hidden) return;
  for (const { source, zones } of groups) {
    const group = document.createElement("div");
    group.className = "dw-area-group";
    group.setAttribute("role", "group");
    group.setAttribute(
      "aria-label",
      `${source.name || source.id} detection area`
    );
    const camera = document.createElement("span");
    camera.className = "dw-area-camera";
    camera.textContent = source.name || source.id;
    group.append(camera, makeAreaTag(source.id, "", "All"));
    for (const zone of zones) {
      group.append(
        makeAreaTag(source.id, zone.uid, zone.name || "Activity area")
      );
    }
    dom.areas.append(group);
  }
}

function renderTags() {
  dom.tags.innerHTML = "";
  const entries = Object.entries(state.classCounts)
    .filter(([name, count]) => name && Number(count) > 0)
    .sort((a, b) => Number(b[1]) - Number(a[1]) || a[0].localeCompare(b[0]));
  const total = entries.reduce((sum, [, count]) => sum + Number(count), 0);
  dom.tags.append(makeTag("", "All", total, state.classes.size === 0));
  for (const [name, count] of entries) {
    dom.tags.append(makeTag(name, classLabel(name), count, state.classes.has(name)));
  }
}

function makeCard(event, cameraName, showCamera = false) {
  const eventCameraName = showCamera
    ? (event.source_name || event.source_id)
    : cameraName;
  const link = document.createElement("a");
  link.className = "dw-card";
  link.href = event.target_url;
  link._preview = event.preview;
  link.dataset.eventId = String(event.id);
  link.setAttribute(
    "aria-label",
    `Open ${classLabel(event.class)} detection from ${eventCameraName} at ${eventTime(event.display_ts)}`
  );

  const thumb = document.createElement("div");
  thumb.className = "dw-thumb";
  const image = document.createElement("img");
  image.src = event.thumb_url;
  image.alt = "";
  image.loading = "lazy";
  image.decoding = "async";
  image.addEventListener("error", () => thumb.classList.add("missing"), { once: true });

  const fallback = document.createElement("span");
  fallback.className = "dw-thumb-fallback";
  fallback.textContent = "No preview";
  const badge = document.createElement("span");
  badge.className = "dw-badge";
  badge.textContent = classLabel(event.class);
  thumb.append(image, fallback, badge);
  if (event.preview) {
    link.classList.add("previewable");
    const previewMark = document.createElement("span");
    previewMark.className = "dw-preview-mark";
    previewMark.title = "Preview available";
    previewMark.setAttribute("aria-hidden", "true");
    previewMark.innerHTML = `<svg width="8" height="8" viewBox="0 0 8 8"><path d="m2 1.25 4.5 2.75L2 6.75v-5.5Z" fill="currentColor"/></svg>`;
    thumb.append(previewMark);
    link.addEventListener("mouseenter", () => queuePreview(link, link._preview));
    link.addEventListener("mouseleave", () => stopPreview(link));
    link.addEventListener("focus", () => queuePreview(link, link._preview));
    link.addEventListener("blur", () => stopPreview(link));
  }

  const meta = document.createElement("div");
  meta.className = "dw-card-meta";
  if (event.provisional) {
    const provisional = document.createElement("span");
    provisional.className = "dw-provisional";
    provisional.title = "Recent detection";
    meta.append(provisional);
  }
  if (showCamera) {
    const source = document.createElement("span");
    source.className = "dw-card-camera";
    source.textContent = event.source_name || event.source_id;
    meta.append(source);
  }
  const time = document.createElement("time");
  time.className = "dw-time";
  time.dateTime = new Date(Number(event.display_ts) * 1000).toISOString();
  time.title = eventTitle(event.display_ts);
  time.textContent = eventTime(event.display_ts);
  const duration = document.createElement("span");
  duration.className = "dw-duration";
  duration.textContent = eventDuration(event);
  meta.append(time, duration);

  link.append(thumb, meta);
  return link;
}

function cameraViewerUrl(camera, live = false) {
  const classes = selectedClasses();
  const params = new URLSearchParams({
    source: camera.id,
    cls: classes.length ? classes.join(",") : "person",
    zone: "none",
  });
  if (live) params.set("live", "1");
  return `/?${params}`;
}

function updateCameraMeta(camera) {
  const section = document.querySelector(`[data-camera-id="${CSS.escape(camera.id)}"]`);
  if (!section) return;
  const count = section.querySelectorAll(".dw-card").length;
  const meta = section.querySelector(".dw-camera-meta");
  if (meta) meta.textContent = count ? `${plural(count, "recent detection")}` : "";
}

function makeLoadMore(camera) {
  const wrap = document.createElement("div");
  wrap.className = "dw-more";
  wrap.hidden = camera.next_before == null;
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "Load older";
  button.addEventListener("click", () => loadMore(camera.id, wrap, button));
  wrap.append(button);
  if (!wrap.hidden) moreObserver?.observe(wrap);
  return wrap;
}

function makeCameraSection(camera) {
  const section = document.createElement("section");
  section.className = "dw-camera";
  section.dataset.cameraId = camera.id;

  const head = document.createElement("header");
  head.className = "dw-camera-head";
  const title = document.createElement("a");
  title.className = `dw-camera-title${camera.record_mode === "live_only" ? " live-only" : ""}`;
  title.href = cameraViewerUrl(camera);
  title.textContent = camera.name;
  const meta = document.createElement("span");
  meta.className = "dw-camera-meta";
  meta.textContent = camera.events.length
    ? plural(camera.events.length, "recent detection")
    : camera.record_mode === "live_only" ? "live only" : "";
  const live = document.createElement("a");
  live.className = "dw-live-link";
  live.href = camera.id === "all"
    ? "/?view=wall"
    : cameraViewerUrl(camera, true);
  live.innerHTML = `<svg width="13" height="13" viewBox="0 0 13 13" fill="none" aria-hidden="true"><circle cx="6.5" cy="6.5" r="2" fill="currentColor"/><circle cx="6.5" cy="6.5" r="4.5" stroke="currentColor"/></svg><span>Live</span>`;
  head.append(title, meta, live);

  const grid = document.createElement("div");
  grid.className = "dw-grid";
  for (const event of camera.events) {
    grid.append(makeCard(event, camera.name, camera.id === "all"));
  }

  if (!camera.events.length) {
    const empty = document.createElement("div");
    empty.className = "dw-camera-empty";
    empty.textContent = camera.id === "all"
      ? "No matching detections across any camera."
      : camera.record_mode === "live_only"
      ? "This camera is live-only, so it does not create detection thumbnails."
      : state.classes.size || state.zones.size
        ? "No matching detections for this camera."
        : "No detections for this camera yet.";
    section.append(head, empty);
  } else {
    section.append(head, grid, makeLoadMore(camera));
  }
  return section;
}

function renderCameras() {
  stopPreview();
  stopFeedObserver();
  moreObserver?.disconnect();
  dom.main.innerHTML = "";
  const cameras = [...state.cameras.values()];
  const visible = cameras.reduce((sum, camera) => sum + camera.events.length, 0);
  const cameraCount = state.camera === "all" ? state.sources.length : cameras.length;
  dom.summary.textContent = `${plural(cameraCount, "camera")} · ${plural(visible, "detection")} shown`;

  if (!state.sources.length) {
    const empty = dom.emptyTemplate.content.cloneNode(true);
    const strong = empty.querySelector("strong");
    const text = empty.querySelector("span");
    strong.textContent = "No cameras";
    text.innerHTML = `Add a camera in <a href="/settings">Settings</a> to start collecting detections.`;
    dom.main.append(empty);
    return;
  }
  if (!cameras.length) {
    const empty = dom.emptyTemplate.content.cloneNode(true);
    dom.main.append(empty);
    return;
  }
  for (const camera of cameras) dom.main.append(makeCameraSection(camera));
  startFeedObserver();
}

async function loadMore(cameraId, wrap, button) {
  const camera = state.cameras.get(cameraId);
  if (!camera || camera.loading || camera.next_before == null) return;
  camera.loading = true;
  button.disabled = true;
  button.textContent = "Loading…";
  try {
    const data = await fetchWall({ source: cameraId, before: camera.next_before });
    const page = data.cameras?.[0];
    if (!page) throw new Error("Camera not found");
    const section = wrap.closest(".dw-camera");
    const grid = section?.querySelector(".dw-grid");
    const known = new Set(camera.events.map(event => String(event.id)));
    const added = (page.events || []).filter(event => !known.has(String(event.id)));
    camera.events.push(...added);
    camera.next_before = page.next_before;
    for (const event of added) {
      const card = makeCard(event, camera.name, camera.id === "all");
      grid?.append(card);
      feedObserver?.observe(card);
    }
    wrap.hidden = camera.next_before == null;
    if (wrap.hidden) moreObserver?.unobserve(wrap);
    updateCameraMeta(camera);
    const shown = [...state.cameras.values()]
      .reduce((sum, item) => sum + item.events.length, 0);
    const cameraCount = state.camera === "all"
      ? state.sources.length
      : state.cameras.size;
    dom.summary.textContent = `${plural(cameraCount, "camera")} · ${plural(shown, "detection")} shown`;
  } catch (error) {
    button.textContent = "Try again";
    button.title = error.message;
    return;
  } finally {
    camera.loading = false;
    button.disabled = false;
    if (!wrap.hidden && button.textContent !== "Try again") button.textContent = "Load older";
  }
}

function renderFailure(error) {
  dom.main.innerHTML = "";
  const empty = dom.emptyTemplate.content.cloneNode(true);
  const root = empty.querySelector(".dw-empty");
  root.classList.add("dw-error");
  root.querySelector("strong").textContent = "Could not load detections";
  root.querySelector("span").textContent = error.message || "The server did not respond.";
  dom.main.append(empty);
  dom.summary.textContent = "Unavailable";
}

function sameFinalizedEpisode(a, b) {
  return (
    Boolean(a?.provisional) !== Boolean(b?.provisional)
    && a?.source_id === b?.source_id
    && a?.class === b?.class
    && Math.abs(Number(a?.abs_ts) - Number(b?.abs_ts)) < .15
  );
}

function mergeRecentEvents(currentEvents, recentEvents) {
  const wantedLength = Math.max(PAGE_SIZE, currentEvents.length);
  const merged = [];
  for (const event of [...recentEvents, ...currentEvents]) {
    const exact = merged.findIndex(item => String(item.id) === String(event.id));
    if (exact >= 0) continue;
    const transitioned = merged.findIndex(item => sameFinalizedEpisode(item, event));
    if (transitioned >= 0) {
      if (merged[transitioned].provisional && !event.provisional) {
        merged[transitioned] = event;
      }
      continue;
    }
    merged.push(event);
  }
  merged.sort((a, b) =>
    Number(b.display_ts) - Number(a.display_ts)
    || String(b.id).localeCompare(String(a.id))
  );
  return merged.slice(0, wantedLength);
}

function eventOrder(events) {
  return events.map(event => String(event.id)).join("\n");
}

function syncRecentCardMetadata(cameraId, events) {
  const section = document.querySelector(
    `[data-camera-id="${CSS.escape(cameraId)}"]`
  );
  if (!section) return;
  const byId = new Map(events.map(event => [String(event.id), event]));
  for (const card of section.querySelectorAll(".dw-card")) {
    const event = byId.get(card.dataset.eventId);
    if (!event) continue;
    card.href = event.target_url;
    card._preview = event.preview;
    const duration = card.querySelector(".dw-duration");
    if (duration) duration.textContent = eventDuration(event);
  }
}

function scheduleRecentRefresh(delay = RECENT_REFRESH_MS) {
  clearTimeout(state.recentTimer);
  if (document.hidden) {
    state.recentTimer = null;
    return;
  }
  state.recentTimer = setTimeout(refreshRecent, delay);
}

async function refreshRecent() {
  clearTimeout(state.recentTimer);
  state.recentTimer = null;
  if (
    document.hidden
    || state.loading
    || state.recentLoading
    || activePreview
    || pendingPreview
  ) {
    scheduleRecentRefresh();
    return;
  }
  state.recentLoading = true;
  const request = state.request;
  try {
    const data = await fetchWall({ limit: 8, counts: false });
    if (
      request !== state.request
      || activePreview
      || pendingPreview
    ) return;
    let layoutChanged = false;
    for (const recentCamera of data.cameras || []) {
      const camera = state.cameras.get(recentCamera.id);
      if (!camera) continue;
      const previousOrder = eventOrder(camera.events);
      camera.events = mergeRecentEvents(
        camera.events,
        recentCamera.events || []
      );
      if (
        camera.next_before != null
        || recentCamera.next_before != null
      ) {
        camera.next_before = camera.events.length
          ? Number(camera.events[camera.events.length - 1].display_ts) - .000001
          : null;
      }
      if (eventOrder(camera.events) !== previousOrder) {
        layoutChanged = true;
      } else {
        syncRecentCardMetadata(camera.id, camera.events);
      }
    }
    if (layoutChanged) renderCameras();
  } catch {
    // A transient poll failure should not replace a usable wall with an error.
  } finally {
    state.recentLoading = false;
    scheduleRecentRefresh();
  }
}

async function loadInitial() {
  clearTimeout(state.recentTimer);
  state.recentTimer = null;
  stopPreview();
  const request = ++state.request;
  state.loading = true;
  dom.refresh.classList.add("loading");
  dom.refresh.disabled = true;
  dom.summary.textContent = "Loading";
  try {
    const data = await fetchWall();
    if (request !== state.request) return;
    state.classCounts = data.classes || {};
    state.sources = data.sources || [];
    state.availableZones = data.zones || [];
    state.zones = new Set(data.selected_zones || []);
    syncUrl({ replace: true });
    state.cameras = new Map(
      (data.cameras || []).map(camera => [camera.id, camera])
    );
    renderCameraTags();
    renderAreaTags();
    renderTags();
    renderCameras();
  } catch (error) {
    if (request === state.request) renderFailure(error);
  } finally {
    if (request === state.request) {
      state.loading = false;
      dom.refresh.classList.remove("loading");
      dom.refresh.disabled = false;
      scheduleRecentRefresh();
    }
  }
}

dom.refresh.addEventListener("click", loadInitial);
dom.viewToggle.addEventListener("click", () => {
  setView(state.view === "feed" ? "grid" : "feed");
});
document.addEventListener("keydown", event => {
  if (
    state.view !== "feed"
    || event.defaultPrevented
    || event.altKey
    || event.ctrlKey
    || event.metaKey
    || event.shiftKey
  ) return;
  const target = event.target;
  if (
    target instanceof Element
    && (
      target.matches("input, textarea, select")
      || target.closest("[contenteditable='true']")
    )
  ) return;
  let direction = 0;
  if (event.key === "ArrowDown" || event.key === "PageDown") direction = 1;
  else if (event.key === "ArrowUp" || event.key === "PageUp") direction = -1;
  if (direction && moveFeed(direction)) event.preventDefault();
});
window.addEventListener("scroll", () => stopPreview(), { passive: true });
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearTimeout(state.recentTimer);
    state.recentTimer = null;
    stopPreview();
  } else {
    scheduleFeedActivation();
    scheduleRecentRefresh(0);
  }
});
window.addEventListener("popstate", () => {
  restoreFiltersFromUrl();
  loadInitial();
});
window.addEventListener("pageshow", event => {
  if (event.persisted) {
    restoreFiltersFromUrl();
    loadInitial();
  }
});

updateViewMode();
loadInitial();
