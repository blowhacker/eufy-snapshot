const test = require("node:test");
const assert = require("node:assert/strict");
const {
  PAYLOAD_UUID,
  decodeUserdataSample,
  LiveSeiClock,
} = require("../src/wanyard/static/live-sei-clock.js");

function crc32(bytes) {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    table[n] = c >>> 0;
  }
  let crc = 0xFFFFFFFF;
  for (const byte of bytes) crc = (table[(crc ^ byte) & 0xFF] ^ (crc >>> 8)) >>> 0;
  return (crc ^ 0xFFFFFFFF) >>> 0;
}

function sample(pts, timestamp, overrides = {}) {
  let value = Math.round(timestamp * 100);
  const bytes = new Uint8Array(9);
  for (let i = 0; i < 8; i++) {
    bytes[i] = value % 256;
    value = Math.floor(value / 256);
  }
  bytes[8] = crc32(bytes.subarray(0, 8)) & 0xFF;
  return { pts, uuid: PAYLOAD_UUID, userDataBytes: bytes, ...overrides };
}

test("decodes the Wanyard unregistered-SEI payload", () => {
  const decoded = decodeUserdataSample(sample(12.5, 1783255587.7));
  assert.deepEqual(decoded, {
    pts: 12.5,
    value: 178325558770,
    timestamp: 1783255587.7,
  });
});

test("rejects foreign UUIDs and corrupt payloads", () => {
  assert.equal(decodeUserdataSample(sample(1, 1000, { uuid: "foreign" })), null);
  const corrupt = sample(1, 1000);
  corrupt.userDataBytes[3] ^= 1;
  assert.equal(decodeUserdataSample(corrupt), null);
});

test("matches the exact presented media frame", () => {
  const clock = new LiveSeiClock();
  clock.ingest([
    sample(10.00, 1000.00),
    sample(10.05, 1000.05),
    sample(10.10, 1000.10),
  ]);
  assert.equal(clock.timestampForMediaTime(10.05), 1000.05);
  assert.equal(clock.timestampForMediaTime(10.0500002), 1000.05);
  assert.equal(clock.timestampForMediaTime(10.075), null);
});

test("deduplicates fragment events and bounds its live cache", () => {
  const clock = new LiveSeiClock({ maxFrames: 100 });
  const samples = [];
  for (let i = 0; i < 120; i++) samples.push(sample(i * 0.05, 1000 + i * 0.05));
  assert.equal(clock.ingest(samples), 120);
  assert.equal(clock.ingest([samples[119]]), 0);
  assert.equal(clock.frames.length, 100);
  assert.equal(clock.timestampForMediaTime(0), null);
  assert.equal(clock.timestampForMediaTime(5.95), 1005.95);
});

test("loads a finalized MP4 frame-clock sidecar", () => {
  const clock = new LiveSeiClock();
  assert.equal(clock.ingestFrameClock([
    [0.1, 178325558770],
    [0.15, 178325558775],
    ["bad", 1],
  ]), 2);
  assert.equal(clock.timestampForMediaTime(0.1), 1783255587.7);
  assert.equal(clock.timestampForMediaTime(0.15), 1783255587.75);
  assert.equal(clock.mediaTimeForTimestamp(1783255587.7), 0.1);
  assert.equal(clock.mediaTimeForTimestamp(1783255587.74), 0.15);
  assert.equal(clock.mediaTimeForTimestamp(1783255590), null);
});

test("inverts a frame clock whose media origin differs from its clock anchor", () => {
  const clock = new LiveSeiClock();
  clock.ingestFrameClock([
    [180.878855556, 178645106921],
    [181.928855556, 178645107026],
    [186.428855556, 178645107476],
  ]);

  assert.equal(
    clock.mediaTimeForTimestamp(1786451070.26),
    181.928855556
  );
  assert.equal(
    clock.timestampForMediaTime(181.928855556),
    1786451070.26
  );
});
