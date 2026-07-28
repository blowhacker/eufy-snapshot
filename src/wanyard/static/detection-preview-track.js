(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.DetectionPreviewTrack = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const MAX_GAP = 2.5;
  const HOLD_SECONDS = 1.0;
  const GATE_FLOOR = 0.22;
  const GATE_SPEED = 2.5;
  const WARM_GATE = 0.40;
  const MIN_SPEED = 0.04;

  function finiteBox(box) {
    if (!box) return false;
    const values = ["x1", "y1", "x2", "y2"].map(key => Number(box[key]));
    return values.every(Number.isFinite)
      && values[2] > values[0]
      && values[3] > values[1];
  }

  function center(box) {
    return {
      x: (Number(box.x1) + Number(box.x2)) / 2,
      y: (Number(box.y1) + Number(box.y2)) / 2,
    };
  }

  function centerDistance(a, b) {
    const ac = center(a);
    const bc = center(b);
    return Math.hypot(ac.x - bc.x, ac.y - bc.y);
  }

  function interpolate(a, b, amount) {
    const mix = key => Number(a[key]) + (Number(b[key]) - Number(a[key])) * amount;
    return {
      ...a,
      x1: mix("x1"),
      y1: mix("y1"),
      x2: mix("x2"),
      y2: mix("y2"),
      conf: Number(a.conf ?? b.conf ?? 0)
        + (Number(b.conf ?? a.conf ?? 0) - Number(a.conf ?? b.conf ?? 0)) * amount,
    };
  }

  // This is the same cheap association used by the main video's moving boxes:
  // mutual nearest neighbours plus a constant-velocity gate. It avoids panning
  // from the selected subject onto another same-class object in a busy frame.
  function buildTracks(detections, wantedClass) {
    const samples = (detections || [])
      .map(detection => ({
        ts: Number(detection.abs_ts),
        boxes: (detection.boxes || []).filter(box =>
          box.cls === wantedClass && finiteBox(box)
        ),
      }))
      .filter(sample => Number.isFinite(sample.ts) && sample.boxes.length)
      .sort((a, b) => a.ts - b.ts);
    const tracks = [];
    for (const sample of samples) {
      const heads = tracks.filter(track => {
        const gap = sample.ts - track.points[track.points.length - 1].ts;
        return gap > 0 && gap <= MAX_GAP;
      });
      const candidates = sample.boxes.map(box => {
        const boxCenter = center(box);
        return {
          box,
          x: boxCenter.x,
          y: boxCenter.y,
          used: false,
        };
      });
      const predictions = heads.map(track => {
        const head = track.points[track.points.length - 1];
        const dt = sample.ts - head.ts;
        const moving = track.vx != null;
        const x = moving ? head.x + track.vx * dt : head.x;
        const y = moving ? head.y + track.vy * dt : head.y;
        const gate = moving
          ? Math.max(GATE_FLOOR, GATE_SPEED * Math.hypot(track.vx, track.vy) * dt)
          : WARM_GATE;
        return {
          track,
          head,
          dt,
          x,
          y,
          gate,
          used: false,
        };
      });

      const pairs = [];
      for (const prediction of predictions) {
        for (const candidate of candidates) {
          const distance = Math.hypot(
            candidate.x - prediction.x,
            candidate.y - prediction.y
          );
          if (distance > prediction.gate) continue;
          if (
            prediction.track.vx != null
            && Math.hypot(prediction.track.vx, prediction.track.vy) > MIN_SPEED
          ) {
            const dx = candidate.x - prediction.head.x;
            const dy = candidate.y - prediction.head.y;
            if (prediction.track.vx * dx + prediction.track.vy * dy < 0) continue;
          }
          pairs.push({ prediction, candidate, distance });
        }
      }
      // Assign the strongest constant-velocity match first, then give the
      // remaining track the remaining box. Requiring mutual nearest heads
      // fragments parallel walkers: the trailing box can be nearer the
      // leading person's old position even though that person's prediction
      // already has an almost-perfect match farther ahead.
      pairs.sort((a, b) => a.distance - b.distance);
      for (const { prediction, candidate } of pairs) {
        if (prediction.used || candidate.used) continue;
        prediction.used = true;
        prediction.track.vx = (candidate.x - prediction.head.x) / prediction.dt;
        prediction.track.vy = (candidate.y - prediction.head.y) / prediction.dt;
        prediction.track.points.push({
          ts: sample.ts,
          box: candidate.box,
          x: candidate.x,
          y: candidate.y,
        });
        candidate.used = true;
      }
      for (const candidate of candidates) {
        if (candidate.used) continue;
        tracks.push({
          vx: null,
          vy: null,
          points: [{
            ts: sample.ts,
            box: candidate.box,
            x: candidate.x,
            y: candidate.y,
          }],
        });
      }
    }
    return tracks;
  }

  function selectTrack(tracks, eventTs, eventBox) {
    if (!finiteBox(eventBox) || !Number.isFinite(Number(eventTs))) return null;
    let selected = null;
    let selectedScore = Infinity;
    for (const track of tracks || []) {
      for (const point of track.points || []) {
        const timeDistance = Math.abs(point.ts - Number(eventTs));
        if (timeDistance > MAX_GAP) continue;
        const score = centerDistance(point.box, eventBox) + timeDistance * 0.03;
        if (score < selectedScore) {
          selected = track;
          selectedScore = score;
        }
      }
    }
    return selected;
  }

  function sampleTrack(track, ts) {
    const time = Number(ts);
    const points = track?.points || [];
    if (!Number.isFinite(time) || !points.length) return null;
    for (let index = 0; index + 1 < points.length; index++) {
      const before = points[index];
      const after = points[index + 1];
      if (
        time >= before.ts
        && time <= after.ts
        && after.ts - before.ts <= MAX_GAP
      ) {
        return interpolate(
          before.box,
          after.box,
          (time - before.ts) / ((after.ts - before.ts) || 1)
        );
      }
    }
    let recent = null;
    for (const point of points) {
      const age = time - point.ts;
      if (age >= -0.15 && age <= HOLD_SECONDS && (!recent || point.ts > recent.ts)) {
        recent = point;
      }
    }
    return recent?.box || null;
  }

  function boxAnchor(box, verticalRatio = 0.5) {
    const ratio = Math.max(0, Math.min(1, Number(verticalRatio)));
    return {
      x: (Number(box.x1) + Number(box.x2)) / 2,
      y: Number(box.y1) + (Number(box.y2) - Number(box.y1)) * ratio,
    };
  }

  function pointVelocity(points, index, verticalRatio) {
    const point = points[index];
    const previous = points[index - 1];
    const next = points[index + 1];
    const hasPrevious = previous && point.ts - previous.ts <= MAX_GAP;
    const hasNext = next && next.ts - point.ts <= MAX_GAP;
    const from = hasPrevious ? previous : point;
    const to = hasNext ? next : point;
    const elapsed = to.ts - from.ts;
    if (elapsed <= 0) return { x: 0, y: 0 };
    const fromAnchor = boxAnchor(from.box, verticalRatio);
    const toAnchor = boxAnchor(to.box, verticalRatio);
    return {
      x: (toAnchor.x - fromAnchor.x) / elapsed,
      y: (toAnchor.y - fromAnchor.y) / elapsed,
    };
  }

  // Recorded previews have the complete track before playback starts. Use a
  // cubic camera path so velocity remains continuous at the sparse detector
  // samples. Box dimensions still interpolate linearly; only the framing
  // anchor follows the smoother path.
  function sampleTrackSmooth(track, ts, verticalRatio = 0.5) {
    const time = Number(ts);
    const points = track?.points || [];
    if (!Number.isFinite(time) || !points.length) return null;
    for (let index = 0; index + 1 < points.length; index++) {
      const before = points[index];
      const after = points[index + 1];
      const elapsed = after.ts - before.ts;
      if (
        time < before.ts
        || time > after.ts
        || elapsed <= 0
        || elapsed > MAX_GAP
      ) continue;
      const amount = (time - before.ts) / elapsed;
      const base = interpolate(before.box, after.box, amount);
      const start = boxAnchor(before.box, verticalRatio);
      const end = boxAnchor(after.box, verticalRatio);
      const startVelocity = pointVelocity(points, index, verticalRatio);
      const endVelocity = pointVelocity(points, index + 1, verticalRatio);
      const amount2 = amount * amount;
      const amount3 = amount2 * amount;
      const h00 = 2 * amount3 - 3 * amount2 + 1;
      const h10 = amount3 - 2 * amount2 + amount;
      const h01 = -2 * amount3 + 3 * amount2;
      const h11 = amount3 - amount2;
      const smooth = {
        x: h00 * start.x
          + h10 * elapsed * startVelocity.x
          + h01 * end.x
          + h11 * elapsed * endVelocity.x,
        y: h00 * start.y
          + h10 * elapsed * startVelocity.y
          + h01 * end.y
          + h11 * elapsed * endVelocity.y,
      };
      // Do not let noisy neighbouring samples make the camera overshoot the
      // two anchors that bound the current frame.
      smooth.x = Math.max(
        Math.min(start.x, end.x),
        Math.min(Math.max(start.x, end.x), smooth.x)
      );
      smooth.y = Math.max(
        Math.min(start.y, end.y),
        Math.min(Math.max(start.y, end.y), smooth.y)
      );
      const baseAnchor = boxAnchor(base, verticalRatio);
      const dx = smooth.x - baseAnchor.x;
      const dy = smooth.y - baseAnchor.y;
      return {
        ...base,
        x1: Number(base.x1) + dx,
        x2: Number(base.x2) + dx,
        y1: Number(base.y1) + dy,
        y2: Number(base.y2) + dy,
      };
    }
    return sampleTrack(track, time);
  }

  function dampCenter(previous, target, elapsed, timeConstant = 0.22) {
    const fallback = {
      x: Number(target?.x),
      y: Number(target?.y),
    };
    if (
      !Number.isFinite(Number(previous?.x))
      || !Number.isFinite(Number(previous?.y))
      || !Number.isFinite(fallback.x)
      || !Number.isFinite(fallback.y)
      || !Number.isFinite(Number(elapsed))
      || Number(elapsed) <= 0
      || Number(elapsed) > 0.75
    ) return fallback;
    const alpha = 1 - Math.exp(
      -Number(elapsed) / Math.max(0.01, Number(timeConstant) || 0.22)
    );
    return {
      x: Number(previous.x) + (fallback.x - Number(previous.x)) * alpha,
      y: Number(previous.y) + (fallback.y - Number(previous.y)) * alpha,
    };
  }

  return {
    buildTracks,
    selectTrack,
    sampleTrack,
    sampleTrackSmooth,
    dampCenter,
  };
});
