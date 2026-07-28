from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
import logging
import secrets

import numpy as np


LOG = logging.getLogger(__name__)

_TRACK_HIGH_CONFIDENCE = 0.35
_TRACK_LOW_CONFIDENCE = 0.10
_TRACK_BUFFER_SECONDS = 5.0
_TRACK_RESET_GRACE_SECONDS = 1.0
_IDENTITY_RESCUE_SECONDS = 1.5
_IDENTITY_MAX_AREA_RATIO = 4.0
_IDENTITY_RESCUE_BASE_DISTANCE = 0.06
_IDENTITY_RESCUE_SPEED_DISTANCE = 0.06
# ByteTrack associates with bounding-box overlap.  At the deliberately sparse
# 2fps detector rate, a narrow walking person can move an entire box width
# between samples and never establish a track.  Dilate only the geometry fed to
# the tracker; the original YOLO boxes are still stored and displayed.
_ASSOCIATION_BOX_SCALE = 2.5


class _AssociationDetections:
    """Minimal Ultralytics-results facade with association-only box padding."""

    def __init__(self, cls, conf, xywh) -> None:
        self.cls = np.asarray(cls)
        self.conf = np.asarray(conf)
        self.xywh = np.asarray(xywh).copy()

    @classmethod
    def from_detections(cls, detections) -> "_AssociationDetections":
        xywh = np.asarray(detections.xywh).copy()
        if len(xywh):
            xywh[:, 2:4] *= _ASSOCIATION_BOX_SCALE
        return cls(detections.cls, detections.conf, xywh)

    def __len__(self) -> int:
        return len(self.cls)

    def __getitem__(self, indices) -> "_AssociationDetections":
        return _AssociationDetections(
            self.cls[indices],
            self.conf[indices],
            self.xywh[indices],
        )


class ByteTrackAssociator:
    """Attach short-lived, per-camera ByteTrack identities to YOLO boxes."""

    low_confidence = _TRACK_LOW_CONFIDENCE

    def __init__(
        self,
        source_id: str,
        fps: float,
        *,
        tracker_factory: Callable[[Any], Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        self.source_id = source_id
        self.fps = max(0.1, float(fps))
        self._tracker_factory = tracker_factory
        self._session_id = session_id or secrets.token_hex(4)
        self._trackers: dict[int, Any] = {}
        self._tokens: dict[Any, str] = {}
        self._token_states: dict[
            str,
            tuple[int, dict, float, tuple[float, float]],
        ] = {}
        self._next_token = 1
        self._last_ts: float | None = None

    def _new_tracker(self):
        factory = self._tracker_factory
        if factory is None:
            from ultralytics.trackers.byte_tracker import BYTETracker

            factory = BYTETracker
        args = SimpleNamespace(
            track_high_thresh=_TRACK_HIGH_CONFIDENCE,
            track_low_thresh=_TRACK_LOW_CONFIDENCE,
            new_track_thresh=_TRACK_HIGH_CONFIDENCE,
            track_buffer=max(1, round(self.fps * _TRACK_BUFFER_SECONDS)),
            match_thresh=0.9,
            # Score fusion makes the fixed 0.7 "unconfirmed track" gate reject
            # moderate-confidence people even when their padded boxes overlap.
            fuse_score=False,
        )
        return factory(args)

    def reset(self) -> None:
        for tracker in self._trackers.values():
            reset = getattr(tracker, "reset", None)
            if callable(reset):
                reset()
        self._trackers.clear()
        self._tokens.clear()
        self._token_states.clear()
        self._last_ts = None

    def _new_token(self) -> str:
        token = (
            f"{self.source_id}:{self._session_id}:"
            f"{self._next_token}"
        )
        self._next_token += 1
        return token

    def _rescue_token(
        self,
        class_id: int,
        box: dict,
        timestamp: float,
        *,
        exclude: str,
        claimed: set[str],
    ) -> str | None:
        best: tuple[float, str] | None = None
        for token, state in self._token_states.items():
            state_class_id, previous, previous_ts, velocity = state
            if token == exclude or token in claimed or state_class_id != class_id:
                continue
            elapsed = timestamp - previous_ts
            if elapsed < 0 or elapsed > _IDENTITY_RESCUE_SECONDS:
                continue
            area_ratio = _box_area_ratio(previous, box)
            if area_ratio is None or area_ratio > _IDENTITY_MAX_AREA_RATIO:
                continue
            previous_center = _box_center(previous)
            current_center = _box_center(box)
            if previous_center is None or current_center is None:
                continue
            predicted_center = (
                previous_center[0] + velocity[0] * elapsed,
                previous_center[1] + velocity[1] * elapsed,
            )
            distance = float(np.hypot(
                predicted_center[0] - current_center[0],
                predicted_center[1] - current_center[1],
            ))
            max_distance = (
                _IDENTITY_RESCUE_BASE_DISTANCE
                + _IDENTITY_RESCUE_SPEED_DISTANCE * elapsed
            )
            if (
                distance > max_distance
            ):
                continue
            score = distance + abs(np.log(area_ratio)) * 0.02
            if best is None or score < best[0]:
                best = (score, token)
        return best[1] if best else None

    def _update_token_state(
        self,
        token: str,
        class_id: int,
        box: dict,
        timestamp: float,
    ) -> None:
        velocity = (0.0, 0.0)
        previous_state = self._token_states.get(token)
        if previous_state is not None and previous_state[0] == class_id:
            _, previous_box, previous_ts, previous_velocity = previous_state
            elapsed = timestamp - previous_ts
            previous_center = _box_center(previous_box)
            current_center = _box_center(box)
            if (
                elapsed > 0
                and elapsed <= _IDENTITY_RESCUE_SECONDS
                and previous_center is not None
                and current_center is not None
            ):
                measured_velocity = (
                    (current_center[0] - previous_center[0]) / elapsed,
                    (current_center[1] - previous_center[1]) / elapsed,
                )
                velocity = (
                    previous_velocity[0] * 0.4 + measured_velocity[0] * 0.6,
                    previous_velocity[1] * 0.4 + measured_velocity[1] * 0.6,
                )
        self._token_states[token] = (
            class_id,
            dict(box),
            timestamp,
            velocity,
        )

    def annotate(
        self,
        detections,
        boxes: list[dict],
        *,
        abs_ts: float,
    ) -> list[dict]:
        """Return accepted boxes with an ephemeral tracker identity.

        ByteTrack is run separately per class so a weak dog detection cannot
        silently turn an existing cat track into a dog track. Tentative tracks
        are included immediately; the episode extractor still requires two
        observations before publishing an event.
        """
        timestamp = float(abs_ts)
        if (
            self._last_ts is not None
            and timestamp - self._last_ts
            > _TRACK_BUFFER_SECONDS + _TRACK_RESET_GRACE_SECONDS
        ):
            self.reset()
        self._last_ts = timestamp
        stale_before = timestamp - (
            _TRACK_BUFFER_SECONDS + _TRACK_RESET_GRACE_SECONDS
        )
        self._token_states = {
            token: state
            for token, state in self._token_states.items()
            if state[2] >= stale_before
        }

        if detections is None:
            return self._fallback(boxes)
        try:
            tracking_detections = _AssociationDetections.from_detections(
                detections
            )
            class_ids = np.asarray(
                tracking_detections.cls, dtype=np.int64
            )
        except Exception:
            LOG.exception("ByteTrack %s received unusable detections", self.source_id)
            return self._fallback(boxes)

        present = {int(value) for value in class_ids.tolist()}
        accepted: dict[int, str] = {}
        claimed: set[str] = set()
        for class_id in sorted(set(self._trackers) | present):
            tracker = self._trackers.get(class_id)
            if tracker is None:
                tracker = self._new_tracker()
                self._trackers[class_id] = tracker
            indices = np.flatnonzero(class_ids == class_id)
            subset = tracking_detections[indices]
            try:
                tracker.update(subset)
            except Exception:
                LOG.exception(
                    "ByteTrack update failed source=%s class_id=%s",
                    self.source_id,
                    class_id,
                )
                return self._fallback(boxes)
            current_tracks = []
            for track in tracker.tracked_stracks:
                if int(track.frame_id) != int(tracker.frame_id):
                    continue
                subset_index = int(track.idx)
                if subset_index < 0 or subset_index >= len(indices):
                    continue
                detection_index = int(indices[subset_index])
                if detection_index >= len(boxes):
                    continue
                box = boxes[detection_index]
                token = self._tokens.get(track)
                state = self._token_states.get(token) if token else None
                area_ratio = (
                    _box_area_ratio(state[1], box)
                    if state is not None
                    else None
                )
                # Preserve credible existing assignments before allowing a
                # newly-created track to claim their canonical identity.
                priority = (
                    0
                    if token and (
                        area_ratio is None
                        or area_ratio <= _IDENTITY_MAX_AREA_RATIO
                    )
                    else 1 if token else 2
                )
                current_tracks.append(
                    (priority, detection_index, track, box, token, area_ratio)
                )

            for (
                _,
                detection_index,
                track,
                box,
                token,
                area_ratio,
            ) in sorted(current_tracks, key=lambda item: item[0]):
                if token is None:
                    token = self._rescue_token(
                        class_id,
                        box,
                        timestamp,
                        exclude="",
                        claimed=claimed,
                    ) or self._new_token()
                    self._tokens[track] = token
                elif (
                    area_ratio is not None
                    and area_ratio > _IDENTITY_MAX_AREA_RATIO
                ):
                    rescued = self._rescue_token(
                        class_id,
                        box,
                        timestamp,
                        exclude=token,
                        claimed=claimed,
                    )
                    token = rescued or self._new_token()
                    self._tokens[track] = token
                if token in claimed:
                    token = self._new_token()
                    self._tokens[track] = token
                accepted[detection_index] = token
                claimed.add(token)
                self._update_token_state(
                    token,
                    class_id,
                    box,
                    timestamp,
                )

        tracked = []
        for index, box in enumerate(boxes):
            token = accepted.get(index)
            if token is None:
                continue
            tracked.append({**box, "track_id": token})
        return tracked

    @staticmethod
    def _fallback(boxes: list[dict]) -> list[dict]:
        return [
            dict(box)
            for box in boxes
            if float(box.get("conf", 0.0)) >= _TRACK_HIGH_CONFIDENCE
        ]


def _box_area_ratio(first: dict, second: dict) -> float | None:
    try:
        areas = [
            max(0.0, float(box["x2"]) - float(box["x1"]))
            * max(0.0, float(box["y2"]) - float(box["y1"]))
            for box in (first, second)
        ]
    except (KeyError, TypeError, ValueError):
        return None
    smaller, larger = sorted(areas)
    if smaller <= 0:
        return None
    return larger / smaller


def _box_center(box: dict) -> tuple[float, float] | None:
    try:
        return (
            (float(box["x1"]) + float(box["x2"])) / 2,
            (float(box["y1"]) + float(box["y2"])) / 2,
        )
    except (KeyError, TypeError, ValueError):
        return None
