(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.WanyardLiveSeiClock = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const PAYLOAD_UUID = "6b616e79-6172-4264-9d74-c3b1c39c05e1";
  const PAYLOAD_BYTES = 9; // unix centiseconds u64 LE + crc8

  let crcTable = null;
  function crc32(bytes) {
    if (!crcTable) {
      crcTable = new Uint32Array(256);
      for (let n = 0; n < 256; n++) {
        let c = n;
        for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
        crcTable[n] = c >>> 0;
      }
    }
    let crc = 0xFFFFFFFF;
    for (const byte of bytes) crc = (crcTable[(crc ^ byte) & 0xFF] ^ (crc >>> 8)) >>> 0;
    return (crc ^ 0xFFFFFFFF) >>> 0;
  }

  function decodeUserdataSample(sample) {
    const pts = Number(sample?.pts);
    const uuid = String(sample?.uuid || "").toLowerCase();
    const raw = sample?.userDataBytes;
    if (!Number.isFinite(pts) || uuid !== PAYLOAD_UUID || raw == null) return null;
    const bytes = raw instanceof Uint8Array
      ? raw
      : new Uint8Array(raw.buffer || raw, raw.byteOffset || 0, raw.byteLength);
    if (bytes.byteLength !== PAYLOAD_BYTES) return null;
    if ((crc32(bytes.subarray(0, 8)) & 0xFF) !== bytes[8]) return null;

    // Current Unix-centisecond values fit safely inside JS's 53-bit integer.
    let value = 0;
    let scale = 1;
    for (let i = 0; i < 8; i++) {
      value += bytes[i] * scale;
      scale *= 256;
    }
    if (!Number.isSafeInteger(value)) return null;
    return { pts, value, timestamp: value / 100 };
  }

  class LiveSeiClock {
    constructor({ maxFrames = 30000, maxMatchSeconds = 0.02 } = {}) {
      this.maxFrames = Math.max(100, Number(maxFrames) || 30000);
      this.maxMatchSeconds = Math.max(0.001, Number(maxMatchSeconds) || 0.02);
      this.reset();
    }

    reset() {
      this.byPts = new Map();
      this.frames = [];
    }

    ingest(samples) {
      let added = 0;
      for (const sample of samples || []) {
        const frame = decodeUserdataSample(sample);
        if (!frame) continue;
        const key = Math.round(frame.pts * 1000000);
        if (!this.byPts.has(key)) added++;
        this.byPts.set(key, frame);
      }
      if (!added) return 0;
      this.frames = [...this.byPts.values()].sort((a, b) => a.pts - b.pts);
      if (this.frames.length > this.maxFrames) {
        this.frames = this.frames.slice(this.frames.length - this.maxFrames);
        this.byPts = new Map(this.frames.map(frame => [Math.round(frame.pts * 1000000), frame]));
      }
      return added;
    }

    match(mediaTime) {
      const target = Number(mediaTime);
      const frames = this.frames;
      if (!Number.isFinite(target) || !frames.length) return null;

      let lo = 0, hi = frames.length;
      while (lo < hi) {
        const mid = (lo + hi) >>> 1;
        if (frames[mid].pts < target) lo = mid + 1;
        else hi = mid;
      }
      const candidates = [];
      if (lo < frames.length) candidates.push({ frame: frames[lo], index: lo });
      if (lo > 0) candidates.push({ frame: frames[lo - 1], index: lo - 1 });
      if (!candidates.length) return null;
      candidates.sort((a, b) => Math.abs(a.frame.pts - target) - Math.abs(b.frame.pts - target));
      const best = candidates[0];

      // Never cross halfway into a neighbouring frame, even when the stream
      // runs faster than the configured absolute tolerance.
      let tolerance = this.maxMatchSeconds;
      const prev = frames[best.index - 1];
      const next = frames[best.index + 1];
      if (prev) tolerance = Math.min(tolerance, (best.frame.pts - prev.pts) * 0.45);
      if (next) tolerance = Math.min(tolerance, (next.pts - best.frame.pts) * 0.45);
      return Math.abs(best.frame.pts - target) <= Math.max(0.001, tolerance)
        ? best.frame
        : null;
    }

    timestampForMediaTime(mediaTime) {
      return this.match(mediaTime)?.timestamp ?? null;
    }
  }

  return { PAYLOAD_UUID, decodeUserdataSample, LiveSeiClock };
});
