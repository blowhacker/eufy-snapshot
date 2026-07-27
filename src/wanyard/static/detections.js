const PAGE_SIZE = 24;

const dom = {
  main: document.getElementById("dwMain"),
  tags: document.getElementById("dwTags"),
  summary: document.getElementById("dwSummary"),
  refresh: document.getElementById("dwRefresh"),
  emptyTemplate: document.getElementById("dwEmptyTemplate"),
};

const state = {
  classes: new Set(
    (new URLSearchParams(location.search).get("classes") || "")
      .split(",")
      .map(value => value.trim())
      .filter(Boolean)
  ),
  classCounts: {},
  cameras: new Map(),
  request: 0,
  loading: false,
};

const moreObserver = "IntersectionObserver" in window
  ? new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const button = entry.target.querySelector("button");
        if (button && !button.disabled) button.click();
      }
    }, { rootMargin: "320px 0px" })
  : null;

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

function syncUrl() {
  const url = new URL(location.href);
  const classes = selectedClasses();
  if (classes.length) url.searchParams.set("classes", classes.join(","));
  else url.searchParams.delete("classes");
  history.replaceState(null, "", url);
}

function apiUrl({ source = null, before = null } = {}) {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
  const classes = selectedClasses();
  if (classes.length) params.set("classes", classes.join(","));
  if (source) params.set("source", source);
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

function makeCard(event, cameraName) {
  const link = document.createElement("a");
  link.className = "dw-card";
  link.href = event.target_url;
  link.dataset.eventId = String(event.id);
  link.setAttribute(
    "aria-label",
    `Open ${classLabel(event.class)} detection from ${cameraName} at ${eventTime(event.display_ts)}`
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

  const meta = document.createElement("div");
  meta.className = "dw-card-meta";
  if (event.provisional) {
    const provisional = document.createElement("span");
    provisional.className = "dw-provisional";
    provisional.title = "Recent detection";
    meta.append(provisional);
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
  const params = new URLSearchParams({
    source: camera.id,
    cls: selectedClasses()[0] || "person",
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
  live.href = cameraViewerUrl(camera, true);
  live.innerHTML = `<svg width="13" height="13" viewBox="0 0 13 13" fill="none" aria-hidden="true"><circle cx="6.5" cy="6.5" r="2" fill="currentColor"/><circle cx="6.5" cy="6.5" r="4.5" stroke="currentColor"/></svg><span>Live</span>`;
  head.append(title, meta, live);

  const grid = document.createElement("div");
  grid.className = "dw-grid";
  for (const event of camera.events) grid.append(makeCard(event, camera.name));

  if (!camera.events.length) {
    const empty = document.createElement("div");
    empty.className = "dw-camera-empty";
    empty.textContent = camera.record_mode === "live_only"
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
  moreObserver?.disconnect();
  dom.main.innerHTML = "";
  const cameras = [...state.cameras.values()];
  const visible = cameras.reduce((sum, camera) => sum + camera.events.length, 0);
  dom.summary.textContent = `${plural(cameras.length, "camera")} · ${plural(visible, "detection")} shown`;

  if (!cameras.length) {
    const empty = dom.emptyTemplate.content.cloneNode(true);
    const strong = empty.querySelector("strong");
    const text = empty.querySelector("span");
    strong.textContent = "No cameras";
    text.innerHTML = `Add a camera in <a href="/settings">Settings</a> to start collecting detections.`;
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
    for (const event of added) grid?.append(makeCard(event, camera.name));
    wrap.hidden = camera.next_before == null;
    if (wrap.hidden) moreObserver?.unobserve(wrap);
    updateCameraMeta(camera);
    const shown = [...state.cameras.values()]
      .reduce((sum, item) => sum + item.events.length, 0);
    dom.summary.textContent = `${plural(state.cameras.size, "camera")} · ${plural(shown, "detection")} shown`;
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
  const request = ++state.request;
  state.loading = true;
  dom.refresh.classList.add("loading");
  dom.refresh.disabled = true;
  dom.summary.textContent = "Loading";
  try {
    const data = await fetchWall();
    if (request !== state.request) return;
    state.classCounts = data.classes || {};
    state.cameras = new Map(
      (data.cameras || []).map(camera => [camera.id, camera])
    );
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
window.addEventListener("pageshow", event => {
  if (event.persisted) loadInitial();
});

loadInitial();
