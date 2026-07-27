const PAGE_SIZE = 24;

function filtersFromUrl() {
  const params = new URLSearchParams(location.search);
  return {
    camera: params.get("camera") || "all",
    classes: new Set(
      (params.get("classes") || "")
        .split(",")
        .map(value => value.trim())
        .filter(Boolean)
    ),
  };
}

const dom = {
  main: document.getElementById("dwMain"),
  cameras: document.getElementById("dwCameras"),
  tags: document.getElementById("dwTags"),
  summary: document.getElementById("dwSummary"),
  refresh: document.getElementById("dwRefresh"),
  emptyTemplate: document.getElementById("dwEmptyTemplate"),
};

const initialFilters = filtersFromUrl();
const state = {
  camera: initialFilters.camera,
  classes: initialFilters.classes,
  classCounts: {},
  sources: [],
  cameras: new Map(),
  request: 0,
  loading: false,
};

const canHoverPreview = window.matchMedia(
  "(hover: hover) and (pointer: fine)"
);
let pendingPreview = null;
let activePreview = null;

const moreObserver = "IntersectionObserver" in window
  ? new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const button = entry.target.querySelector("button");
        if (button && !button.disabled) button.click();
      }
    }, { rootMargin: "320px 0px" })
  : null;

function previewCrop(box, frameWidth, frameHeight, aspect = 4 / 3) {
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
  const padding = Math.max(24, Math.max(boxWidth, boxHeight) * .45);
  let cropWidth = boxWidth + padding * 2;
  let cropHeight = boxHeight + padding * 2;
  if (cropWidth / cropHeight < aspect) cropWidth = cropHeight * aspect;
  else cropHeight = cropWidth / aspect;
  cropWidth = Math.min(frameWidth, Math.max(96, cropWidth));
  cropHeight = Math.min(frameHeight, Math.max(72, cropHeight));
  if (cropWidth / cropHeight < aspect) {
    cropWidth = Math.min(frameWidth, cropHeight * aspect);
  } else {
    cropHeight = Math.min(frameHeight, cropWidth / aspect);
  }
  const width = Math.min(frameWidth, Math.max(2, Math.round(cropWidth)));
  const height = Math.min(frameHeight, Math.max(2, Math.round(cropHeight)));
  return {
    x: Math.round(Math.max(0, Math.min(frameWidth - width, centerX - width / 2))),
    y: Math.round(Math.max(0, Math.min(frameHeight - height, centerY - height / 2))),
    width,
    height,
  };
}

function layoutActivePreview() {
  if (!activePreview) return;
  const { video, host, preview } = activePreview;
  const frameWidth = video.videoWidth;
  const frameHeight = video.videoHeight;
  const hostWidth = host.clientWidth;
  const hostHeight = host.clientHeight;
  if (!frameWidth || !frameHeight || !hostWidth || !hostHeight) return;
  const crop = previewCrop(preview.box, frameWidth, frameHeight);
  if (!crop) return;
  const scale = Math.max(hostWidth / crop.width, hostHeight / crop.height);
  video.style.width = `${frameWidth * scale}px`;
  video.style.height = `${frameHeight * scale}px`;
  video.style.left = `${
    -crop.x * scale + (hostWidth - crop.width * scale) / 2
  }px`;
  video.style.top = `${
    -crop.y * scale + (hostHeight - crop.height * scale) / 2
  }px`;
}

function cancelPendingPreview(card = null) {
  if (!pendingPreview || (card && pendingPreview.card !== card)) return;
  const pendingCard = pendingPreview.card;
  clearTimeout(pendingPreview.timer);
  pendingPreview = null;
  pendingCard.classList.remove("preview-loading");
}

function stopPreview(card = null, failed = false) {
  cancelPendingPreview(card);
  if (!activePreview || (card && activePreview.card !== card)) return;
  const { card: activeCard, video, resizeObserver } = activePreview;
  activePreview = null;
  resizeObserver?.disconnect();
  activeCard.classList.remove("preview-loading", "preview-playing");
  if (failed) activeCard.dataset.previewFailed = "1";
  try {
    video.pause();
    video.removeAttribute("src");
    video.load();
  } catch {}
  video.remove();
}

function startPreview(card, preview) {
  pendingPreview = null;
  if (
    !canHoverPreview.matches
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
  };
  activePreview = item;

  const playPreview = () => {
    if (activePreview !== item) return;
    video.play().catch(() => stopPreview(card, true));
  };
  video.addEventListener("loadedmetadata", () => {
    if (activePreview !== item || !Number.isFinite(video.duration)) return;
    item.start = Math.min(item.start, Math.max(0, video.duration - .1));
    item.end = Math.min(
      video.duration,
      Math.max(item.start + .25, item.end)
    );
    layoutActivePreview();
    if (item.start > .01) {
      video.addEventListener("seeked", playPreview, { once: true });
      video.currentTime = item.start;
    } else {
      playPreview();
    }
  }, { once: true });
  video.addEventListener("playing", () => {
    if (activePreview !== item) return;
    card.classList.remove("preview-loading");
    card.classList.add("preview-playing");
  });
  video.addEventListener("waiting", () => {
    if (activePreview === item) card.classList.add("preview-loading");
  });
  video.addEventListener("timeupdate", () => {
    if (
      activePreview === item
      && video.currentTime >= item.end - .05
    ) {
      video.currentTime = item.start;
      video.play().catch(() => {});
    }
  });
  video.addEventListener("ended", () => {
    if (activePreview !== item) return;
    video.currentTime = item.start;
    video.play().catch(() => {});
  });
  video.addEventListener("error", () => stopPreview(card, true), { once: true });
  if ("ResizeObserver" in window) {
    item.resizeObserver = new ResizeObserver(layoutActivePreview);
    item.resizeObserver.observe(host);
  }
  host.append(video);
  video.src = preview.url;
  video.load();
}

function queuePreview(card, preview) {
  if (!preview || !canHoverPreview.matches || card.dataset.previewFailed) return;
  if (activePreview?.card === card) return;
  cancelPendingPreview();
  card.classList.add("preview-loading");
  pendingPreview = {
    card,
    timer: setTimeout(() => startPreview(card, preview), 180),
  };
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
  history[replace ? "replaceState" : "pushState"](
    { detectionWall: true },
    "",
    url
  );
}

function restoreFiltersFromUrl() {
  const filters = filtersFromUrl();
  state.camera = filters.camera;
  state.classes = filters.classes;
}

function apiUrl({ source = state.camera, before = null } = {}) {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
  const classes = selectedClasses();
  if (classes.length) params.set("classes", classes.join(","));
  params.set("source", source || "all");
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
    previewMark.title = "Hover to preview";
    previewMark.setAttribute("aria-hidden", "true");
    previewMark.innerHTML = `<svg width="8" height="8" viewBox="0 0 8 8"><path d="m2 1.25 4.5 2.75L2 6.75v-5.5Z" fill="currentColor"/></svg>`;
    thumb.append(previewMark);
    link.addEventListener("mouseenter", () => queuePreview(link, event.preview));
    link.addEventListener("mouseleave", () => stopPreview(link));
    link.addEventListener("focus", () => queuePreview(link, event.preview));
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
      : state.classes.size
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
      grid?.append(makeCard(event, camera.name, camera.id === "all"));
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

async function loadInitial() {
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
    state.cameras = new Map(
      (data.cameras || []).map(camera => [camera.id, camera])
    );
    renderCameraTags();
    renderTags();
    renderCameras();
  } catch (error) {
    if (request === state.request) renderFailure(error);
  } finally {
    if (request === state.request) {
      state.loading = false;
      dom.refresh.classList.remove("loading");
      dom.refresh.disabled = false;
    }
  }
}

dom.refresh.addEventListener("click", loadInitial);
window.addEventListener("scroll", () => stopPreview(), { passive: true });
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopPreview();
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

loadInitial();
