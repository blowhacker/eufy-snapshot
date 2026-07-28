const test = require("node:test");
const assert = require("node:assert/strict");

const {
  buildTracks,
  selectTrack,
  sampleTrack,
  dampCenter,
} = require("../src/wanyard/static/detection-preview-track.js");

function box(cls, centerX, centerY = .5) {
  return {
    cls,
    x1: centerX - .02,
    y1: centerY - .05,
    x2: centerX + .02,
    y2: centerY + .05,
  };
}

test("tracks and interpolates the event subject without switching people", () => {
  const detections = [
    { abs_ts: 10, boxes: [box("person", .10), box("person", .80)] },
    { abs_ts: 11, boxes: [box("person", .20), box("person", .79)] },
    { abs_ts: 12, boxes: [box("person", .30), box("person", .78)] },
    { abs_ts: 13, boxes: [box("person", .40), box("person", .77)] },
  ];
  const tracks = buildTracks(detections, "person");
  const track = selectTrack(tracks, 11, box("person", .20));
  const sampled = sampleTrack(track, 12.5);

  assert.ok(track);
  assert.equal(track.points.length, 4);
  assert.ok(Math.abs((sampled.x1 + sampled.x2) / 2 - .35) < .000001);
});

test("keeps close parallel people as two continuous tracks", () => {
  const detections = [
    { abs_ts: 30, boxes: [box("person", .45), box("person", .40)] },
    { abs_ts: 30.5, boxes: [box("person", .50), box("person", .45)] },
    { abs_ts: 31, boxes: [box("person", .55), box("person", .50)] },
    { abs_ts: 31.5, boxes: [box("person", .60), box("person", .55)] },
  ];
  const tracks = buildTracks(detections, "person")
    .filter(track => track.points.length > 1);

  assert.equal(tracks.length, 2);
  assert.deepEqual(
    tracks.map(track => track.points.length).sort(),
    [4, 4]
  );
});

test("filters other classes and drops a stale pan target", () => {
  const tracks = buildTracks([
    { abs_ts: 20, boxes: [box("dog", .2), box("person", .6)] },
  ], "person");
  const track = selectTrack(tracks, 20, box("person", .6));

  assert.equal(tracks.length, 1);
  assert.equal(sampleTrack(track, 20.5).cls, "person");
  assert.equal(sampleTrack(track, 21.1), null);
});

test("damps box-centre noise and resets after a seek", () => {
  const first = dampCenter({ x: .2, y: .5 }, { x: .4, y: .6 }, .04);

  assert.ok(first.x > .2 && first.x < .25);
  assert.ok(first.y > .5 && first.y < .525);
  assert.deepEqual(
    dampCenter(first, { x: .1, y: .3 }, -.5),
    { x: .1, y: .3 }
  );
});
