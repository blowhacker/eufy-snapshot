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
    observability = _temporal_observability(candidates)
    readiness, reasons = classify_feasibility(analysis.metrics)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_image(cv2, output_dir / "left.jpg", left_native)
    _write_image(cv2, output_dir / "right.jpg", right_native)
    matches_image = _draw_matches(cv2, left_image, right_image, analysis)
    _write_image(cv2, output_dir / "matches.jpg", matches_image)
    epipolar_image = _draw_epipolar(cv2, np, left_image, right_image, analysis)
    if epipolar_image is not None:
        _write_image(cv2, output_dir / "epipolar.jpg", epipolar_image)

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
        "temporal_offset": observability,
        "candidates": candidates,
        "artifacts": {
            "left": "left.jpg",
            "right": "right.jpg",
            "matches": "matches.jpg",
            "epipolar": "epipolar.jpg" if epipolar_image is not None else None,
        },
        "limitations": [
            "This estimates uncalibrated two-view overlap; it does not produce depth.",
            "Matched-feature coverage is not a measurement of the complete shared field of view.",
            "A static scene cannot establish inter-camera exposure synchronization.",
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
