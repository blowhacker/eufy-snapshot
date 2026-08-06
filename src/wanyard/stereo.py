"""Read-only two-camera feasibility inspection.

This module intentionally stops before stereo calibration or depth.  It asks a
smaller question: do two timestamp-addressed Wanyard sources contain enough
shared visual structure to justify calibrating them as a stereo pair?

Frames are always obtained through :mod:`wanyard.media_time`; this command
never opens another RTSP connection and never treats filenames or container
timestamps as scene time.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from . import media_time


class StereoInspectError(RuntimeError):
    """A user-facing feasibility inspection failure."""


@dataclass(frozen=True)
class MatchMetrics:
    left_keypoints: int
    right_keypoints: int
    ratio_matches: int
    fundamental_inliers: int
    inlier_ratio: float
    left_grid_coverage: float
    right_grid_coverage: float
    median_epipolar_error_px: float | None
    score: float


@dataclass
class _Analysis:
    metrics: MatchMetrics
    left_keypoints: list[Any]
    right_keypoints: list[Any]
    matches: list[Any]
    inlier_mask: Any | None
    fundamental: Any | None
    left_points: Any | None
    right_points: Any | None


@dataclass(frozen=True)
class VehicleSample:
    timestamp: float
    x: float
    y: float
    area: float


@dataclass(frozen=True)
class VehicleTrack:
    uid: str
    source_id: str
    cls: str
    samples: tuple[VehicleSample, ...]
    travel: float

    @property
    def start(self) -> float:
        return self.samples[0].timestamp

    @property
    def end(self) -> float:
        return self.samples[-1].timestamp


@dataclass(frozen=True)
class _VehiclePair:
    left: VehicleTrack
    right: VehicleTrack
    best_offset_ms: int
    median_error_px: float
    p90_error_px: float
    samples: int
    sharpness: float
    curve: tuple[tuple[int, float], ...]


def build_offsets_ms(minimum: int, maximum: int, step: int) -> list[int]:
    if step <= 0:
        raise StereoInspectError("offset step must be greater than zero")
    if minimum > maximum:
        raise StereoInspectError("offset minimum must not exceed maximum")
    if maximum - minimum > 5000:
        raise StereoInspectError("offset search window must not exceed 5000 ms")
    offsets = list(range(minimum, maximum + 1, step))
    if maximum not in offsets:
        offsets.append(maximum)
    if minimum <= 0 <= maximum and 0 not in offsets:
        offsets.append(0)
    offsets = sorted(set(offsets))
    if len(offsets) > 201:
        raise StereoInspectError("offset search must contain no more than 201 candidates")
    return offsets


def latest_common_timestamp(video_db, left_source: str, right_source: str,
                            edge_margin_seconds: float = 2.0) -> float:
    left = video_db.segment_bounds(left_source)
    right = video_db.segment_bounds(right_source)
    if not left or not right:
        missing = left_source if not left else right_source
        raise StereoInspectError(f"no timestamped video coverage for source {missing!r}")
    start = max(float(left["from"]), float(right["from"]))
    end = min(float(left["to"]), float(right["to"]))
    if end <= start:
        raise StereoInspectError("the two sources have no overlapping video coverage")
    # Open segments can extend the public bounds to "now" while their
    # authoritative MP4 frame is still pending.  Default inspection should be
    # deterministic, so choose the newest overlap between finalized segments.
    # Two hours comfortably spans Wanyard's ten-minute segment cadence without
    # loading the full retention history.
    window_start = max(start, end - 7200.0)
    left_segments = video_db.list_segments(
        left_source, since=window_start, until=end
    )
    right_segments = video_db.list_segments(
        right_source, since=window_start, until=end
    )
    overlaps: list[tuple[float, float]] = []
    for left_row in left_segments:
        left_coverage = _closed_segment_coverage(left_row)
        if left_coverage is None:
            continue
        for right_row in right_segments:
            right_coverage = _closed_segment_coverage(right_row)
            if right_coverage is None:
                continue
            overlap_start = max(left_coverage[0], right_coverage[0])
            overlap_end = min(left_coverage[1], right_coverage[1])
            if overlap_end > overlap_start:
                overlaps.append((overlap_start, overlap_end))
    if not overlaps:
        raise StereoInspectError(
            "no shared finalized segment coverage in the latest two hours;"
            " pass --at to attempt a specific live timestamp"
        )
    overlap_start, overlap_end = max(overlaps, key=lambda value: value[1])
    candidate = overlap_end - max(0.0, float(edge_margin_seconds))
    return max(overlap_start, candidate)


def _closed_segment_coverage(row: dict) -> tuple[float, float] | None:
    if row.get("end_ts") is None or row.get("media_epoch") is None:
        return None
    start = float(row["media_epoch"])
    duration = row.get("duration_sec")
    if duration is None:
        duration = float(row["end_ts"]) - float(row["start_ts"])
    end = start + max(0.0, float(duration))
    return (start, end) if end > start else None


def inspect_pair(
    video_db,
    video_dir: Path,
    left_source: str,
    right_source: str,
    at: float,
    offsets_ms: Iterable[int],
    output_dir: Path,
    *,
    max_dimension: int = 1280,
    timing_window_seconds: float = 10800.0,
    timing_step_ms: int = 10,
    max_vehicle_events: int = 30,
) -> dict:
    """Inspect a source pair and write a JSON/image diagnostic bundle."""
    cv2 = _load_cv2()
    np = _load_numpy()

    if not left_source or not right_source:
        raise StereoInspectError("both source ids are required")
    if left_source == right_source:
        raise StereoInspectError("left and right sources must be different")
    if not math.isfinite(float(at)) or float(at) <= 0:
        raise StereoInspectError("inspection timestamp must be a positive Unix timestamp")
    if max_dimension < 320 or max_dimension > 4096:
        raise StereoInspectError("max dimension must be between 320 and 4096 pixels")
    if timing_window_seconds < 60 or timing_window_seconds > 86400:
        raise StereoInspectError("timing window must be between 60 and 86400 seconds")
    if timing_step_ms < 5 or timing_step_ms > 250:
        raise StereoInspectError("timing step must be between 5 and 250 ms")
    if max_vehicle_events < 5 or max_vehicle_events > 100:
        raise StereoInspectError("vehicle event limit must be between 5 and 100")

    offsets = list(offsets_ms)
    if not offsets:
        raise StereoInspectError("at least one temporal offset is required")

    left_result = _read_frame(video_db, video_dir, left_source, float(at))
    if left_result.frame is None:
        raise StereoInspectError(
            f"left frame unavailable at {at:.3f}: {left_result.status}"
        )
    left_native = left_result.frame
    left_image, left_scale = _resize_for_analysis(
        cv2, left_native, max_dimension
    )

    candidates: list[dict] = []
    best: tuple[int, Any, Any, _Analysis, Any] | None = None
    for offset_ms in offsets:
        right_ts = float(at) + float(offset_ms) / 1000.0
        right_result = _read_frame(video_db, video_dir, right_source, right_ts)
        if right_result.frame is None:
            candidates.append({
                "offset_ms": int(offset_ms),
                "right_timestamp": right_ts,
                "status": right_result.status,
                "provider": right_result.provider,
                "metrics": None,
            })
            continue
        right_image, right_scale = _resize_for_analysis(
            cv2, right_result.frame, max_dimension
        )
        analysis = _analyze_images(cv2, np, left_image, right_image)
        candidates.append({
            "offset_ms": int(offset_ms),
            "right_timestamp": right_ts,
            "status": "ok",
            "provider": right_result.provider,
            "metrics": asdict(analysis.metrics),
        })
        if best is None or analysis.metrics.score > best[3].metrics.score:
            best = (
                int(offset_ms), right_result.frame, right_image, analysis,
                right_scale,
            )

    if best is None:
        statuses = sorted({str(c["status"]) for c in candidates})
        raise StereoInspectError(
            "no right frame was available in the offset window"
            f" (statuses: {', '.join(statuses)})"
        )

    best_offset, right_native, right_image, analysis, right_scale = best
    static_observability = _temporal_observability(candidates)
    readiness, reasons = classify_feasibility(analysis.metrics)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timing_offsets = build_offsets_ms(
        min(offsets), max(offsets), timing_step_ms
    )
    timing, timing_pairs = _analyze_vehicle_timing(
        video_db,
        np,
        analysis.fundamental,
        left_source,
        right_source,
        float(at) - float(timing_window_seconds),
        float(at),
        timing_offsets,
        (left_image.shape[1], left_image.shape[0]),
        (right_image.shape[1], right_image.shape[0]),
        maximum_events=max_vehicle_events,
    )
    _write_image(cv2, output_dir / "left.jpg", left_native)
    _write_image(cv2, output_dir / "right.jpg", right_native)
    matches_image = _draw_matches(cv2, left_image, right_image, analysis)
    _write_image(cv2, output_dir / "matches.jpg", matches_image)
    epipolar_image = _draw_epipolar(cv2, np, left_image, right_image, analysis)
    if epipolar_image is not None:
        _write_image(cv2, output_dir / "epipolar.jpg", epipolar_image)
    timing_plot = _draw_timing_plot(cv2, np, timing, timing_offsets)
    _write_image(cv2, output_dir / "timing-offsets.jpg", timing_plot)
    montage = _draw_vehicle_montage(
        cv2,
        video_db,
        video_dir,
        timing_pairs,
        max_rows=6,
    )
    if montage is not None:
        _write_image(cv2, output_dir / "timing-vehicles.jpg", montage)

    report = {
        "schema_version": 1,
        "left_source": left_source,
        "right_source": right_source,
        "left_timestamp": float(at),
        "best_right_timestamp": float(at) + best_offset / 1000.0,
        "best_offset_ms": best_offset,
        "left_provider": left_result.provider,
        "right_provider": next(
            c["provider"] for c in candidates
            if c["offset_ms"] == best_offset and c["status"] == "ok"
        ),
        "left_native_size": _image_size(left_native),
        "right_native_size": _image_size(right_native),
        "left_analysis_scale": left_scale,
        "right_analysis_scale": right_scale,
        "max_analysis_dimension": int(max_dimension),
        "feasibility": readiness,
        "feasibility_reasons": reasons,
        "best_metrics": asdict(analysis.metrics),
        "temporal_offset": timing,
        "static_offset_scan": static_observability,
        "candidates": candidates,
        "artifacts": {
            "left": "left.jpg",
            "right": "right.jpg",
            "matches": "matches.jpg",
            "epipolar": "epipolar.jpg" if epipolar_image is not None else None,
            "timing_offsets": "timing-offsets.jpg",
            "timing_vehicles": (
                "timing-vehicles.jpg" if montage is not None else None
            ),
        },
        "limitations": [
            "This estimates uncalibrated two-view overlap; it does not produce depth.",
            "Matched-feature coverage is not a measurement of the complete shared field of view.",
            "Vehicle timing estimates ingestion-time alignment, not hardware shutter synchronization.",
            "Calibration is still required before epipolar lines become horizontal or depth is metric.",
        ],
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def classify_feasibility(metrics: MatchMetrics) -> tuple[str, list[str]]:
    coverage = min(metrics.left_grid_coverage, metrics.right_grid_coverage)
    reasons: list[str] = []
    if metrics.fundamental_inliers < 25:
        reasons.append("fewer than 25 geometrically consistent feature matches")
    if metrics.inlier_ratio < 0.20:
        reasons.append("less than 20% of candidate matches agree on two-view geometry")
    if coverage < 0.08:
        reasons.append("consistent matches cover less than 8% of one image grid")

    if reasons:
        return "weak", reasons
    if (
        metrics.fundamental_inliers >= 80
        and metrics.inlier_ratio >= 0.40
        and coverage >= 0.20
    ):
        return "promising", [
            "shared structure is numerous, geometrically consistent, and spatially distributed"
        ]
    return "borderline", [
        "shared structure is detectable, but calibration images should confirm usable overlap"
    ]


def _read_frame(video_db, video_dir: Path, source_id: str, timestamp: float):
    with video_db._connect() as conn:
        return media_time.read_frame(conn, video_dir, source_id, timestamp)


def _load_cv2():
    try:
        import cv2
    except ModuleNotFoundError as exc:
        raise StereoInspectError(
            "OpenCV is not installed; install the project runtime dependencies"
        ) from exc
    return cv2


def _load_numpy():
    try:
        import numpy as np
    except ModuleNotFoundError as exc:
        raise StereoInspectError(
            "NumPy is not installed; install the project runtime dependencies"
        ) from exc
    return np


def _resize_for_analysis(cv2, image, max_dimension: int):
    height, width = image.shape[:2]
    largest = max(height, width)
    if largest <= max_dimension:
        return image.copy(), 1.0
    scale = max_dimension / float(largest)
    resized = cv2.resize(
        image,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )
    return resized, scale


def _create_detector(cv2):
    if hasattr(cv2, "SIFT_create"):
        return cv2.SIFT_create(nfeatures=5000), cv2.NORM_L2, "sift"
    return cv2.ORB_create(nfeatures=5000), cv2.NORM_HAMMING, "orb"


def _analyze_images(cv2, np, left, right) -> _Analysis:
    detector, norm, _detector_name = _create_detector(cv2)
    left_gray = cv2.cvtColor(left, cv2.COLOR_BGR2GRAY)
    right_gray = cv2.cvtColor(right, cv2.COLOR_BGR2GRAY)
    # The two CCTV sensors can have radically different exposure curves.  Local
    # contrast normalization retains physical edges while making descriptors
    # less sensitive to a bright/flat image from one camera.
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    left_gray = clahe.apply(left_gray)
    right_gray = clahe.apply(right_gray)
    # Both cameras burn an unrelated text overlay into the top edge.  Matching
    # identical clock glyphs would manufacture correspondences at infinity, so
    # exclude that narrow carrier band from geometric evidence.
    left_mask = np.full(left_gray.shape, 255, dtype=np.uint8)
    right_mask = np.full(right_gray.shape, 255, dtype=np.uint8)
    left_mask[:max(1, round(left_gray.shape[0] * 0.10)), :] = 0
    right_mask[:max(1, round(right_gray.shape[0] * 0.10)), :] = 0
    left_kp, left_desc = detector.detectAndCompute(left_gray, left_mask)
    right_kp, right_desc = detector.detectAndCompute(right_gray, right_mask)
    left_kp = list(left_kp or [])
    right_kp = list(right_kp or [])

    if left_desc is None or right_desc is None or not left_kp or not right_kp:
        return _empty_analysis(len(left_kp), len(right_kp))

    matcher = cv2.BFMatcher(norm, crossCheck=False)
    forward_raw = matcher.knnMatch(left_desc, right_desc, k=2)
    reverse_raw = matcher.knnMatch(right_desc, left_desc, k=2)
    forward = [
        pair[0] for pair in forward_raw
        if len(pair) == 2 and pair[0].distance < 0.78 * pair[1].distance
    ]
    reverse_pairs = {
        (pair[0].trainIdx, pair[0].queryIdx)
        for pair in reverse_raw
        if len(pair) == 2 and pair[0].distance < 0.78 * pair[1].distance
    }
    matches = [
        match for match in forward
        if (match.queryIdx, match.trainIdx) in reverse_pairs
    ]
    if len(matches) < 8:
        return _empty_analysis(len(left_kp), len(right_kp), matches)

    left_points = np.float32([left_kp[m.queryIdx].pt for m in matches])
    right_points = np.float32([right_kp[m.trainIdx].pt for m in matches])
    robust_method = getattr(cv2, "USAC_MAGSAC", cv2.FM_RANSAC)
    fundamental, mask = cv2.findFundamentalMat(
        left_points, right_points, robust_method, 1.5, 0.999
    )
    if fundamental is None or mask is None:
        return _empty_analysis(len(left_kp), len(right_kp), matches)
    fundamental = np.asarray(fundamental, dtype=np.float64)
    if fundamental.shape[0] > 3:
        fundamental = fundamental[:3, :3]
    if fundamental.shape != (3, 3):
        return _empty_analysis(len(left_kp), len(right_kp), matches)

    inlier_mask = np.asarray(mask).reshape(-1).astype(bool)
    if len(inlier_mask) != len(matches):
        return _empty_analysis(len(left_kp), len(right_kp), matches)
    inlier_count = int(inlier_mask.sum())
    inlier_ratio = inlier_count / len(matches) if matches else 0.0
    inlier_left = left_points[inlier_mask]
    inlier_right = right_points[inlier_mask]
    left_coverage = _grid_coverage(inlier_left, left.shape[1], left.shape[0])
    right_coverage = _grid_coverage(inlier_right, right.shape[1], right.shape[0])
    epi_error = _median_epipolar_error(np, fundamental, inlier_left, inlier_right)
    coverage = min(left_coverage, right_coverage)
    error_penalty = min(10.0, epi_error or 0.0)
    score = max(0.0, (
        inlier_count * (0.5 + inlier_ratio) * (0.5 + coverage)
        - error_penalty
    ))
    metrics = MatchMetrics(
        left_keypoints=len(left_kp),
        right_keypoints=len(right_kp),
        ratio_matches=len(matches),
        fundamental_inliers=inlier_count,
        inlier_ratio=round(inlier_ratio, 6),
        left_grid_coverage=round(left_coverage, 6),
        right_grid_coverage=round(right_coverage, 6),
        median_epipolar_error_px=(round(epi_error, 6) if epi_error is not None else None),
        score=round(float(score), 6),
    )
    return _Analysis(
        metrics, left_kp, right_kp, matches, inlier_mask, fundamental,
        left_points, right_points,
    )


def _empty_analysis(left_count: int, right_count: int,
                    matches: list[Any] | None = None) -> _Analysis:
    metrics = MatchMetrics(
        left_keypoints=left_count,
        right_keypoints=right_count,
        ratio_matches=len(matches or []),
        fundamental_inliers=0,
        inlier_ratio=0.0,
        left_grid_coverage=0.0,
        right_grid_coverage=0.0,
        median_epipolar_error_px=None,
        score=0.0,
    )
    return _Analysis(metrics, [], [], matches or [], None, None, None, None)


def _grid_coverage(points, width: int, height: int,
                   columns: int = 8, rows: int = 6) -> float:
    if points is None or len(points) == 0 or width <= 0 or height <= 0:
        return 0.0
    occupied: set[tuple[int, int]] = set()
    for point in points:
        x, y = float(point[0]), float(point[1])
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        column = min(columns - 1, max(0, int(x / width * columns)))
        row = min(rows - 1, max(0, int(y / height * rows)))
        occupied.add((column, row))
    return len(occupied) / float(columns * rows)


def _median_epipolar_error(np, fundamental, left_points, right_points):
    if left_points is None or len(left_points) == 0:
        return None
    ones = np.ones((len(left_points), 1), dtype=np.float64)
    left_h = np.hstack([left_points.astype(np.float64), ones])
    right_h = np.hstack([right_points.astype(np.float64), ones])
    right_lines = (fundamental @ left_h.T).T
    left_lines = (fundamental.T @ right_h.T).T
    numerators = np.abs(np.sum(right_h * right_lines, axis=1))
    right_den = np.linalg.norm(right_lines[:, :2], axis=1)
    left_den = np.linalg.norm(left_lines[:, :2], axis=1)
    valid = (right_den > 1e-12) & (left_den > 1e-12)
    if not bool(valid.any()):
        return None
    errors = 0.5 * (
        numerators[valid] / right_den[valid]
        + numerators[valid] / left_den[valid]
    )
    return float(np.median(errors))


def _temporal_observability(candidates: list[dict]) -> dict:
    valid = [
        c for c in candidates
        if c.get("metrics") is not None and c["metrics"]["score"] > 0
    ]
    if len(valid) < 3:
        return {
            "observable": False,
            "suggested_offset_ms": None,
            "reason": "fewer than three offsets produced valid geometry",
        }
    ranked = sorted(valid, key=lambda c: c["metrics"]["score"], reverse=True)
    best_score = float(ranked[0]["metrics"]["score"])
    second_score = float(ranked[1]["metrics"]["score"])
    median_score = float(statistics.median(c["metrics"]["score"] for c in valid))
    decisive = (
        best_score >= median_score * 1.25
        and best_score >= second_score * 1.10
    )
    if not decisive:
        return {
            "observable": False,
            "suggested_offset_ms": None,
            "reason": "offset scores have no decisive peak; the scene may be too static",
            "best_candidate_offset_ms": int(ranked[0]["offset_ms"]),
        }
    return {
        "observable": True,
        "suggested_offset_ms": int(ranked[0]["offset_ms"]),
        "reason": "one offset has a substantially stronger geometric-consistency score",
    }


_VEHICLE_CLASSES = {"car", "truck", "bus", "motorcycle"}


def _vehicle_tracks(
    rows: Iterable[dict],
    source_id: str,
    *,
    minimum_samples: int = 4,
    minimum_travel: float = 0.035,
    maximum_gap_seconds: float = 1.6,
    maximum_duration_seconds: float = 45.0,
) -> list[VehicleTrack]:
    """Build contiguous moving tracks from persisted per-frame YOLO boxes."""
    grouped: dict[str, list[tuple[str, VehicleSample]]] = {}
    for row in rows:
        try:
            timestamp = float(row["abs_ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(timestamp):
            continue
        for box in row.get("boxes") or []:
            cls = str(box.get("cls") or "").strip().lower()
            track_id = str(box.get("track_id") or "").strip()
            if cls not in _VEHICLE_CLASSES or not track_id:
                continue
            try:
                x1, y1 = float(box["x1"]), float(box["y1"])
                x2, y2 = float(box["x2"]), float(box["y2"])
                confidence = float(box.get("conf", 1.0))
            except (KeyError, TypeError, ValueError):
                continue
            values = (x1, y1, x2, y2, confidence)
            if not all(math.isfinite(value) for value in values):
                continue
            if confidence < 0.30 or x2 <= x1 or y2 <= y1:
                continue
            sample = VehicleSample(
                timestamp=timestamp,
                x=max(0.0, min(1.0, (x1 + x2) * 0.5)),
                # Garden-old sees moving vehicles above a wall while the
                # higher camera sees their full height.  Box centres therefore
                # represent different physical points.  The upper-body anchor
                # remains visible in both views and is much closer to a true
                # epipolar correspondence.
                y=max(0.0, min(1.0, y1 + (y2 - y1) * 0.20)),
                area=max(0.0, min(1.0, (x2 - x1) * (y2 - y1))),
            )
            grouped.setdefault(track_id, []).append((cls, sample))

    tracks: list[VehicleTrack] = []
    for track_id, tagged_samples in grouped.items():
        tagged_samples.sort(key=lambda item: item[1].timestamp)
        episodes: list[list[tuple[str, VehicleSample]]] = [[]]
        for tagged in tagged_samples:
            current = episodes[-1]
            if current:
                previous = current[-1][1]
                gap = tagged[1].timestamp - previous.timestamp
                jump = math.hypot(
                    tagged[1].x - previous.x,
                    tagged[1].y - previous.y,
                )
                if gap > maximum_gap_seconds or jump > 0.30:
                    episodes.append([])
            episodes[-1].append(tagged)

        for episode_index, episode in enumerate(episodes):
            samples = tuple(item[1] for item in episode)
            if len(samples) < minimum_samples:
                continue
            duration = samples[-1].timestamp - samples[0].timestamp
            if duration <= 0.0 or duration > maximum_duration_seconds:
                continue
            xs = [sample.x for sample in samples]
            ys = [sample.y for sample in samples]
            travel = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
            if travel < minimum_travel:
                continue
            cls = max(
                {item[0] for item in episode},
                key=lambda value: sum(1 for item in episode if item[0] == value),
            )
            tracks.append(VehicleTrack(
                uid=f"{track_id}:{episode_index}",
                source_id=source_id,
                cls=cls,
                samples=samples,
                travel=round(travel, 6),
            ))
    return sorted(tracks, key=lambda track: (track.start, track.uid))


def _interpolate_track(track: VehicleTrack, timestamps, np):
    source_t = np.asarray(
        [sample.timestamp for sample in track.samples], dtype=np.float64
    )
    target_t = np.asarray(timestamps, dtype=np.float64)
    x = np.interp(
        target_t, source_t,
        np.asarray([sample.x for sample in track.samples], dtype=np.float64),
    )
    y = np.interp(
        target_t, source_t,
        np.asarray([sample.y for sample in track.samples], dtype=np.float64),
    )
    return np.column_stack([x, y])


def _symmetric_epipolar_errors(np, fundamental, left_points, right_points):
    ones = np.ones((len(left_points), 1), dtype=np.float64)
    left_h = np.hstack([left_points.astype(np.float64), ones])
    right_h = np.hstack([right_points.astype(np.float64), ones])
    right_lines = (fundamental @ left_h.T).T
    left_lines = (fundamental.T @ right_h.T).T
    numerators = np.abs(np.sum(right_h * right_lines, axis=1))
    right_den = np.linalg.norm(right_lines[:, :2], axis=1)
    left_den = np.linalg.norm(left_lines[:, :2], axis=1)
    valid = (right_den > 1e-12) & (left_den > 1e-12)
    errors = np.full(len(left_points), np.inf, dtype=np.float64)
    errors[valid] = 0.5 * (
        numerators[valid] / right_den[valid]
        + numerators[valid] / left_den[valid]
    )
    return errors


def _vehicle_pair_curve(
    np,
    fundamental,
    left: VehicleTrack,
    right: VehicleTrack,
    offsets_ms: Iterable[int],
    left_size: tuple[int, int],
    right_size: tuple[int, int],
) -> _VehiclePair | None:
    left_width, left_height = left_size
    right_width, right_height = right_size
    curve: list[tuple[int, float]] = []
    details: dict[int, tuple[float, int]] = {}
    left_times = np.asarray(
        [sample.timestamp for sample in left.samples], dtype=np.float64
    )
    left_normalized = np.asarray(
        [(sample.x, sample.y) for sample in left.samples], dtype=np.float64
    )
    right_start, right_end = right.start, right.end

    for offset_ms in offsets_ms:
        shifted = left_times + float(offset_ms) / 1000.0
        valid = (shifted >= right_start) & (shifted <= right_end)
        if int(valid.sum()) < 4:
            continue
        left_points = left_normalized[valid].copy()
        right_points = _interpolate_track(right, shifted[valid], np)
        left_points[:, 0] *= left_width
        left_points[:, 1] *= left_height
        right_points[:, 0] *= right_width
        right_points[:, 1] *= right_height
        errors = _symmetric_epipolar_errors(
            np, fundamental, left_points, right_points
        )
        errors = errors[np.isfinite(errors)]
        if len(errors) < 4:
            continue
        median_error = float(np.median(errors))
        curve.append((int(offset_ms), median_error))
        details[int(offset_ms)] = (
            float(np.percentile(errors, 90)), int(len(errors))
        )

    if len(curve) < 3:
        return None
    best_offset, best_error = min(curve, key=lambda item: item[1])
    p90_error, sample_count = details[best_offset]
    steps = [
        abs(curve[index][0] - curve[index - 1][0])
        for index in range(1, len(curve))
    ]
    shoulder_distance = max(100, (min(steps) if steps else 10) * 3)
    shoulder_errors = [
        error for offset, error in curve
        if abs(offset - best_offset) >= shoulder_distance
    ]
    shoulder = (
        float(statistics.median(shoulder_errors))
        if shoulder_errors else float(statistics.median(error for _, error in curve))
    )
    sharpness = shoulder / max(best_error, 0.01)
    return _VehiclePair(
        left=left,
        right=right,
        best_offset_ms=best_offset,
        median_error_px=round(best_error, 6),
        p90_error_px=round(p90_error, 6),
        samples=sample_count,
        sharpness=round(sharpness, 6),
        curve=tuple((offset, round(error, 6)) for offset, error in curve),
    )


def _summarize_vehicle_pairs(np, pairs: list[_VehiclePair]) -> dict:
    if not pairs:
        return {
            "observable": False,
            "suggested_offset_ms": None,
            "confidence": "low",
            "dynamic_3d_ready": False,
            "reason": "no unambiguous shared moving-vehicle tracks were found",
            "matched_events": 0,
            "robust_jitter_ms": None,
            "p95_residual_ms": None,
            "events": [],
        }

    offsets = np.asarray(
        [pair.best_offset_ms for pair in pairs], dtype=np.float64
    )
    initial_median = float(np.median(offsets))
    absolute = np.abs(offsets - initial_median)
    mad = float(np.median(absolute))
    cutoff = max(30.0, mad * 3.0)
    kept = [
        pair for pair in pairs
        if abs(pair.best_offset_ms - initial_median) <= cutoff
    ]
    offsets = np.asarray(
        [pair.best_offset_ms for pair in kept], dtype=np.float64
    )
    median = float(np.median(offsets))
    residuals = np.abs(offsets - median)
    jitter = 1.4826 * float(np.median(residuals))
    p95 = float(np.percentile(residuals, 95))
    median_sharpness = float(np.median([pair.sharpness for pair in kept]))
    event_count = len(kept)
    dynamic_ready = (
        event_count >= 20
        and p95 <= 80.0
        and median_sharpness >= 1.15
    )
    observable = event_count >= 5 and p95 <= 250.0
    if dynamic_ready:
        confidence = "high"
        reason = "vehicle timing passes the dynamic-stereo offset and jitter gate"
    elif event_count >= 8 and p95 <= 150.0 and median_sharpness >= 1.08:
        confidence = "medium"
        reason = "offset is measurable, but more events or lower jitter are required"
    elif observable:
        confidence = "low"
        reason = "a likely offset is visible, but the event distribution is too weak"
    else:
        confidence = "low"
        reason = "vehicle-derived offsets do not form a sufficiently tight consensus"

    events = [{
        "left_track": pair.left.uid,
        "right_track": pair.right.uid,
        "class": pair.left.cls,
        "left_start": pair.left.start,
        "left_end": pair.left.end,
        "right_start": pair.right.start,
        "right_end": pair.right.end,
        "offset_ms": pair.best_offset_ms,
        "median_epipolar_error_px": pair.median_error_px,
        "p90_epipolar_error_px": pair.p90_error_px,
        "samples": pair.samples,
        "sharpness": pair.sharpness,
        "curve": [
            {"offset_ms": offset, "median_error_px": error}
            for offset, error in pair.curve
        ],
    } for pair in kept]
    return {
        "observable": observable,
        "suggested_offset_ms": int(round(median)),
        "confidence": confidence,
        "dynamic_3d_ready": dynamic_ready,
        "reason": reason,
        "matched_events": event_count,
        "robust_jitter_ms": round(jitter, 3),
        "p95_residual_ms": round(p95, 3),
        "median_curve_sharpness": round(median_sharpness, 6),
        "thresholds": {
            "minimum_events": 20,
            "maximum_p95_residual_ms": 80,
            "minimum_median_curve_sharpness": 1.15,
        },
        "events": events,
    }


def _analyze_vehicle_timing(
    video_db,
    np,
    fundamental,
    left_source: str,
    right_source: str,
    since: float,
    until: float,
    offsets_ms: Iterable[int],
    left_size: tuple[int, int],
    right_size: tuple[int, int],
    maximum_events: int = 30,
) -> tuple[dict, list[_VehiclePair]]:
    if fundamental is None or not hasattr(video_db, "detections_between"):
        report = _summarize_vehicle_pairs(np, [])
        report["reason"] = "vehicle timing requires geometry and per-frame detections"
        return report, []

    left_rows = video_db.detections_between(left_source, since, until)
    right_rows = video_db.detections_between(right_source, since, until)
    left_tracks = _vehicle_tracks(left_rows, left_source)
    right_tracks = _vehicle_tracks(right_rows, right_source)
    # High-travel tracks carry a much clearer timing signal and cap the
    # quadratic pairing work when a busy road produces tracker fragments.
    left_tracks = sorted(left_tracks, key=lambda track: track.travel, reverse=True)[:1800]
    right_tracks = sorted(right_tracks, key=lambda track: track.travel, reverse=True)[:1800]
    offsets = sorted(set(int(offset) for offset in offsets_ms))
    search_margin = max(abs(offsets[0]), abs(offsets[-1])) / 1000.0

    candidates: list[_VehiclePair] = []
    for left in left_tracks:
        for right in right_tracks:
            if left.end + search_margin < right.start:
                continue
            if right.end + search_margin < left.start:
                continue
            pair = _vehicle_pair_curve(
                np, fundamental, left, right, offsets, left_size, right_size
            )
            if pair is None:
                continue
            if pair.best_offset_ms in {offsets[0], offsets[-1]}:
                continue
            if (
                pair.median_error_px > 14.0
                or pair.p90_error_px > 28.0
                or pair.sharpness < 1.05
            ):
                continue
            candidates.append(pair)

    candidates.sort(key=lambda pair: (
        pair.median_error_px / max(pair.sharpness, 1.0),
        -pair.samples,
    ))
    accepted: list[_VehiclePair] = []
    used_left: set[str] = set()
    used_right: set[str] = set()
    for pair in candidates:
        if pair.left.uid in used_left or pair.right.uid in used_right:
            continue
        accepted.append(pair)
        used_left.add(pair.left.uid)
        used_right.add(pair.right.uid)
        if len(accepted) >= maximum_events:
            break

    report = _summarize_vehicle_pairs(np, accepted)
    report.update({
        "window": {"since": float(since), "until": float(until)},
        "left_moving_tracks": len(left_tracks),
        "right_moving_tracks": len(right_tracks),
        "candidate_pairs": len(candidates),
        "offset_convention": "right_timestamp = left_timestamp + offset",
    })
    kept_ids = {
        (event["left_track"], event["right_track"])
        for event in report["events"]
    }
    kept = [
        pair for pair in accepted
        if (pair.left.uid, pair.right.uid) in kept_ids
    ]
    return report, kept


def _draw_timing_plot(cv2, np, timing: dict, offsets_ms: list[int]):
    width, height = 1100, 560
    canvas = np.full((height, width, 3), (248, 248, 248), dtype=np.uint8)
    left, right, top, bottom = 90, width - 35, 95, height - 70
    cv2.rectangle(canvas, (left, top), (right, bottom), (70, 70, 70), 1)
    minimum, maximum = min(offsets_ms), max(offsets_ms)
    span = max(1, maximum - minimum)

    def x_for(offset):
        return int(round(left + (float(offset) - minimum) / span * (right - left)))

    for offset in offsets_ms:
        if offset == 0 or offset % 100 == 0:
            x = x_for(offset)
            color = (100, 100, 100) if offset == 0 else (215, 215, 215)
            cv2.line(canvas, (x, top), (x, bottom), color, 1)
            cv2.putText(
                canvas, f"{offset:+d}", (x - 22, bottom + 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (60, 60, 60), 1,
                cv2.LINE_AA,
            )

    events = timing.get("events") or []
    for index, event in enumerate(events):
        y = int(round(
            top + (index + 0.5) / max(1, len(events)) * (bottom - top)
        ))
        x = x_for(event["offset_ms"])
        cv2.circle(canvas, (x, y), 5, (34, 120, 220), -1, cv2.LINE_AA)
        cv2.line(canvas, (left, y), (right, y), (235, 235, 235), 1)

    suggested = timing.get("suggested_offset_ms")
    if suggested is not None:
        x = x_for(suggested)
        cv2.line(canvas, (x, top), (x, bottom), (40, 170, 40), 3)
    title = "Moving-vehicle temporal alignment"
    cv2.putText(
        canvas, title, (35, 38), cv2.FONT_HERSHEY_SIMPLEX,
        0.85, (25, 25, 25), 2, cv2.LINE_AA,
    )
    summary = (
        f"events={timing.get('matched_events', 0)}  "
        f"offset={suggested if suggested is not None else 'n/a'} ms  "
        f"jitter={timing.get('robust_jitter_ms')} ms  "
        f"p95={timing.get('p95_residual_ms')} ms  "
        f"confidence={timing.get('confidence', 'low')}"
    )
    cv2.putText(
        canvas, summary, (35, 68), cv2.FONT_HERSHEY_SIMPLEX,
        0.52, (45, 45, 45), 1, cv2.LINE_AA,
    )
    cv2.putText(
        canvas, "right timestamp = left timestamp + offset (ms)",
        (left, height - 20), cv2.FONT_HERSHEY_SIMPLEX,
        0.5, (55, 55, 55), 1, cv2.LINE_AA,
    )
    if not events:
        cv2.putText(
            canvas, timing.get("reason", "No timing evidence"),
            (left + 40, (top + bottom) // 2), cv2.FONT_HERSHEY_SIMPLEX,
            0.65, (50, 50, 160), 2, cv2.LINE_AA,
        )
    return canvas


def _draw_vehicle_montage(
    cv2,
    video_db,
    video_dir: Path,
    pairs: list[_VehiclePair],
    *,
    max_rows: int,
):
    import numpy as np

    rows = []
    for pair in pairs[:max_rows]:
        offset_seconds = pair.best_offset_ms / 1000.0
        start = max(pair.left.start, pair.right.start - offset_seconds)
        end = min(pair.left.end, pair.right.end - offset_seconds)
        if end <= start:
            continue
        left_ts = (start + end) * 0.5
        right_ts = left_ts + offset_seconds
        left_result = _read_frame(
            video_db, video_dir, pair.left.source_id, left_ts
        )
        right_result = _read_frame(
            video_db, video_dir, pair.right.source_id, right_ts
        )
        if left_result.frame is None or right_result.frame is None:
            continue
        left_image, _ = _resize_for_analysis(cv2, left_result.frame, 640)
        right_image, _ = _resize_for_analysis(cv2, right_result.frame, 640)
        left_point = _interpolate_track(pair.left, [left_ts], np)[0]
        right_point = _interpolate_track(pair.right, [right_ts], np)[0]
        cv2.circle(
            left_image,
            (int(left_point[0] * left_image.shape[1]),
             int(left_point[1] * left_image.shape[0])),
            9, (40, 230, 40), 2, cv2.LINE_AA,
        )
        cv2.circle(
            right_image,
            (int(right_point[0] * right_image.shape[1]),
             int(right_point[1] * right_image.shape[0])),
            9, (40, 230, 40), 2, cv2.LINE_AA,
        )
        row = _side_by_side(cv2, left_image, right_image)
        cv2.putText(
            row,
            f"offset {pair.best_offset_ms:+d} ms  "
            f"error {pair.median_error_px:.1f}px  "
            f"sharpness {pair.sharpness:.2f}x",
            (16, 28), cv2.FONT_HERSHEY_SIMPLEX,
            0.65, (20, 20, 20), 3, cv2.LINE_AA,
        )
        cv2.putText(
            row,
            f"offset {pair.best_offset_ms:+d} ms  "
            f"error {pair.median_error_px:.1f}px  "
            f"sharpness {pair.sharpness:.2f}x",
            (16, 28), cv2.FONT_HERSHEY_SIMPLEX,
            0.65, (235, 235, 235), 1, cv2.LINE_AA,
        )
        rows.append(row)
    if not rows:
        return None
    widest = max(row.shape[1] for row in rows)
    padded = [
        cv2.copyMakeBorder(
            row, 0, 0, 0, widest - row.shape[1],
            cv2.BORDER_CONSTANT, value=(20, 20, 20),
        )
        for row in rows
    ]
    return np.vstack(padded)


def _draw_matches(cv2, left, right, analysis: _Analysis):
    if not analysis.left_keypoints or not analysis.right_keypoints:
        return _side_by_side(cv2, left, right)
    if analysis.inlier_mask is None:
        selected = analysis.matches[:100]
    else:
        selected = [
            match for match, is_inlier in zip(analysis.matches, analysis.inlier_mask)
            if bool(is_inlier)
        ][:150]
    return cv2.drawMatches(
        left, analysis.left_keypoints,
        right, analysis.right_keypoints,
        selected, None,
        matchColor=(40, 220, 40),
        singlePointColor=(80, 80, 80),
        flags=cv2.DrawMatchesFlags_NOT_DRAW_SINGLE_POINTS,
    )


def _draw_epipolar(cv2, np, left, right, analysis: _Analysis):
    if (
        analysis.fundamental is None
        or analysis.inlier_mask is None
        or analysis.left_points is None
        or int(analysis.inlier_mask.sum()) == 0
    ):
        return None
    left_points = analysis.left_points[analysis.inlier_mask]
    right_points = analysis.right_points[analysis.inlier_mask]
    if len(left_points) > 20:
        indices = np.linspace(0, len(left_points) - 1, 20, dtype=int)
        left_points = left_points[indices]
        right_points = right_points[indices]
    right_lines = cv2.computeCorrespondEpilines(
        left_points.reshape(-1, 1, 2), 1, analysis.fundamental
    ).reshape(-1, 3)
    left_lines = cv2.computeCorrespondEpilines(
        right_points.reshape(-1, 1, 2), 2, analysis.fundamental
    ).reshape(-1, 3)
    left_draw = left.copy()
    right_draw = right.copy()
    for index, (left_point, right_point, left_line, right_line) in enumerate(
        zip(left_points, right_points, left_lines, right_lines)
    ):
        color = (
            int((53 * index + 47) % 205 + 50),
            int((97 * index + 71) % 205 + 50),
            int((151 * index + 29) % 205 + 50),
        )
        _draw_line(cv2, left_draw, left_line, color)
        _draw_line(cv2, right_draw, right_line, color)
        cv2.circle(left_draw, tuple(np.int32(left_point)), 5, color, -1)
        cv2.circle(right_draw, tuple(np.int32(right_point)), 5, color, -1)
    return _side_by_side(cv2, left_draw, right_draw)


def _draw_line(cv2, image, line, color) -> None:
    a, b, c = (float(value) for value in line)
    height, width = image.shape[:2]
    if abs(b) > 1e-9:
        start = (0, int(round(-c / b)))
        end = (width - 1, int(round(-(c + a * (width - 1)) / b)))
    elif abs(a) > 1e-9:
        x = int(round(-c / a))
        start, end = (x, 0), (x, height - 1)
    else:
        return
    cv2.line(image, start, end, color, 1, cv2.LINE_AA)


def _side_by_side(cv2, left, right):
    import numpy as np

    height = max(left.shape[0], right.shape[0])
    left_pad = cv2.copyMakeBorder(
        left, 0, height - left.shape[0], 0, 0,
        cv2.BORDER_CONSTANT, value=(20, 20, 20),
    )
    right_pad = cv2.copyMakeBorder(
        right, 0, height - right.shape[0], 0, 0,
        cv2.BORDER_CONSTANT, value=(20, 20, 20),
    )
    return np.hstack([left_pad, right_pad])


def _write_image(cv2, path: Path, image) -> None:
    if not cv2.imwrite(str(path), image):
        raise StereoInspectError(f"failed to write diagnostic image {path}")


def _image_size(image) -> dict:
    height, width = image.shape[:2]
    return {"width": int(width), "height": int(height)}
