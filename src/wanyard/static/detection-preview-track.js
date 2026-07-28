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
          bestHead: null,
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
          best: null,
          bestDistance: Infinity,
        };
      });

      for (const prediction of predictions) {
        for (const candidate of candidates) {
          const distance = Math.hypot(
            candidate.x - prediction.x,
            candidate.y - prediction.y
          );
          if (distance < prediction.bestDistance) {
            prediction.bestDistance = distance;
            prediction.best = candidate;
          }
        }
      }
      for (const candidate of candidates) {
        let bestDistance = Infinity;
        for (const prediction of predictions) {
          const distance = Math.hypot(
            candidate.x - prediction.head.x,
            candidate.y - prediction.head.y
          );
          if (distance < bestDistance) {
            bestDistance = distance;
            candidate.bestHead = prediction;
          }
        }
      }
      for (const prediction of predictions) {
        const candidate = prediction.best;
        if (
          !candidate
          || candidate.used
          || candidate.bestHead !== prediction
          || prediction.bestDistance > prediction.gate
        ) continue;
        if (
          prediction.track.vx != null
          && Math.hypot(prediction.track.vx, prediction.track.vy) > MIN_SPEED
        ) {
          const dx = candidate.x - prediction.head.x;
          const dy = candidate.y - prediction.head.y;
          if (prediction.track.vx * dx + prediction.track.vy * dy < 0) continue;
        }
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

  return { buildTracks, selectTrack, sampleTrack, dampCenter };
});
