const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const source = fs.readFileSync(
  path.join(__dirname, "..", "src", "wanyard", "static", "video2.js"),
  "utf8",
);

function between(start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from + start.length);
  assert.notEqual(from, -1, `missing source marker: ${start}`);
  assert.notEqual(to, -1, `missing source marker: ${end}`);
  return source.slice(from, to);
}

test("notification navigation lands media before loading the historical timeline", () => {
  const openNotification = between(
    "async function openNotification(notification)",
    "async function markAllNotificationsRead()",
  );
  const seek = openNotification.indexOf("await seekToTimestamp(");
  const load = openNotification.indexOf("const loading = load();");

  assert.ok(seek >= 0, "notification path should seek to its timestamp");
  assert.ok(load > seek, "historical loading must start after the media seek");
  assert.match(openNotification, /st\.timelineAutoFollow = false/);
  assert.match(openNotification, /centerWindowOn\(ts, \{ scheduleLoad: false \}\)/);
  assert.match(openNotification, /frameClock: "lazy"/);
});

test("explicit timeline loads cancel their pending debounced duplicate", () => {
  const setTimelineWindow = between(
    "function setTimelineWindow(from, to",
    "function centerWindowOn(",
  );

  assert.match(setTimelineWindow, /scheduleLoad = true/);
  assert.match(setTimelineWindow, /else if \(!scheduleLoad\)/);
  assert.match(setTimelineWindow, /clearTimeout\(_fetchDebounce\)/);
});

test("background refresh does not pile onto a slow historical load", () => {
  const autoRefresh = between(
    "// ── Auto-refresh",
    'window.addEventListener("resize"',
  );

  assert.match(autoRefresh, /if \(_loadCount > 0\) return/);
  assert.match(source, /const NOTIFICATION_POLL_MS = 30000/);
});
