"""Bounded, read-only overlap checks for a prospective spatial camera set.

This deliberately reuses the inexpensive still-image geometry from
``wanyard.stereo`` but does not run temporal offset searches, detection scans,
or write diagnostic artefacts.  It is intended for the first gate in the
spatial-view creation flow.
"""

from __future__ import annotations

from dataclasses import asdict
from itertools import combinations
from pathlib import Path
import time

from . import stereo


class SpatialFeasibilityError(ValueError):
    """The requested camera set is not a valid bounded inspection."""


def inspect_camera_set(
    video_db,
    video_dir: Path,
    camera_ids,
    *,
    max_dimension: int = 960,
) -> dict:
    """Return a compact, JSON-safe overlap report for two to sixteen cameras.

    Each pair gets its newest shared finalized timestamp and exactly one
    zero-offset geometry analysis.  A failed pair remains in the report, so a
    single unavailable camera cannot hide useful results for the other pairs.
    """
    cameras = _validate_camera_ids(camera_ids)
    if max_dimension < 320 or max_dimension > 4096:
        raise SpatialFeasibilityError("max dimension must be between 320 and 4096 pixels")

    pairs = [_inspect_pair(video_db, Path(video_dir), left, right, max_dimension)
             for left, right in combinations(cameras, 2)]
    components = _connected_components(cameras, pairs)
    mergeable = len(components) == 1
    return {
        "camera_ids": cameras,
        "mergeable": mergeable,
        "status": "mergeable" if mergeable else "disconnected",
        "pairs": pairs,
        "components": components,
        "checked_at": time.time(),
    }


def _validate_camera_ids(camera_ids) -> list[str]:
    if isinstance(camera_ids, (str, bytes)):
        raise SpatialFeasibilityError("camera ids must be a sequence")
    try:
        cameras = list(camera_ids)
    except TypeError as exc:
        raise SpatialFeasibilityError("camera ids must be a sequence") from exc
    if not 2 <= len(cameras) <= 16:
        raise SpatialFeasibilityError("select between 2 and 16 cameras")
    if any(not isinstance(camera, str) or not camera.strip() for camera in cameras):
        raise SpatialFeasibilityError("camera ids must be non-empty strings")
    if len(set(cameras)) != len(cameras):
        raise SpatialFeasibilityError("camera ids must be unique")
    return cameras


def _inspect_pair(video_db, video_dir: Path, left: str, right: str,
                  max_dimension: int) -> dict:
    pair = {"left_camera_id": left, "right_camera_id": right}
    try:
        timestamp = stereo.latest_common_timestamp(video_db, left, right)
        left_frame = stereo._read_frame(video_db, video_dir, left, timestamp)
        right_frame = stereo._read_frame(video_db, video_dir, right, timestamp)
        if left_frame.frame is None or right_frame.frame is None:
            unavailable = []
            if left_frame.frame is None:
                unavailable.append(f"{left}: {left_frame.status}")
            if right_frame.frame is None:
                unavailable.append(f"{right}: {right_frame.status}")
            raise stereo.StereoInspectError("frame unavailable (" + "; ".join(unavailable) + ")")
        analysis = _analyze_frames(left_frame.frame, right_frame.frame, max_dimension)
        status, reasons = stereo.classify_feasibility(analysis.metrics)
        pair.update({
            "timestamp": float(timestamp),
            "status": status,
            "reasons": reasons,
            "metrics": asdict(analysis.metrics),
        })
    except Exception as exc:  # Pair failures are report data, not a set failure.
        pair.update({"timestamp": None, "status": "error", "reasons": [str(exc)], "metrics": None})
    return pair


def _analyze_frames(left_frame, right_frame, max_dimension: int):
    cv2 = stereo._load_cv2()
    np = stereo._load_numpy()
    left, _ = stereo._resize_for_analysis(cv2, left_frame, max_dimension)
    right, _ = stereo._resize_for_analysis(cv2, right_frame, max_dimension)
    return stereo._analyze_images(cv2, np, left, right)


def _connected_components(camera_ids: list[str], pairs: list[dict]) -> list[list[str]]:
    """Return deterministic components using promising/borderline pair edges."""
    neighbours = {camera: set() for camera in camera_ids}
    for pair in pairs:
        if pair.get("status") not in {"promising", "borderline"}:
            continue
        left, right = pair["left_camera_id"], pair["right_camera_id"]
        neighbours[left].add(right)
        neighbours[right].add(left)
    remaining = set(camera_ids)
    components: list[list[str]] = []
    for start in camera_ids:
        if start not in remaining:
            continue
        stack, component = [start], []
        remaining.remove(start)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbour in neighbours[current]:
                if neighbour in remaining:
                    remaining.remove(neighbour)
                    stack.append(neighbour)
        components.append([camera for camera in camera_ids if camera in component])
    return components
