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

test("zone canvas edits only the explicitly selected area", () => {
  const pointerDown = between(
    'el.zoneCanvas?.addEventListener("pointerdown"',
    'el.zoneCanvas?.addEventListener("pointermove"',
  );

  assert.doesNotMatch(pointerDown, /zoneUnderPointer|selectZone\s*\(/);
  assert.match(pointerDown, /selectedPoints\s*\(\)/);
});

test("zone name always follows an explicit selection change", () => {
  const chrome = between(
    "function updateZoneChrome()",
    "function selectZone(",
  );

  assert.doesNotMatch(chrome, /document\.activeElement/);
  assert.match(chrome, /el\.zoneName\.value\s*=\s*nextName/);
});

test("zone editor toolbar is draggable only from its dedicated handle", () => {
  const listeners = between(
    'el.zonePrev?.addEventListener("click"',
    'el.zoneName?.addEventListener("input"',
  );

  assert.match(listeners, /zoneDragHandle\?\.addEventListener\("pointerdown", startZoneBarDrag\)/);
  assert.doesNotMatch(listeners, /zoneBar\?\.addEventListener\("pointerdown"/);
});

test("zone editor toolbar drag is clamped and keyboard accessible", () => {
  const drag = between(
    "function zoneBarOffsetInsideStage(",
    "function setZoneEditing(",
  );

  assert.match(drag, /getBoundingClientRect\(\)/);
  assert.match(drag, /setPointerCapture/);
  assert.match(drag, /ArrowLeft/);
  assert.match(drag, /event\.key === "Home"/);
});
