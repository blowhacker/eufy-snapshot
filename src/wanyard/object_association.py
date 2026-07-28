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
            match_thresh=0.8,
            fuse_score=True,
        )
        return factory(args)

    def reset(self) -> None:
        for tracker in self._trackers.values():
            reset = getattr(tracker, "reset", None)
            if callable(reset):
                reset()
        self._trackers.clear()
        self._tokens.clear()
        self._last_ts = None

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

        if detections is None:
            return self._fallback(boxes)
        try:
            class_ids = np.asarray(detections.cls, dtype=np.int64)
        except Exception:
            LOG.exception("ByteTrack %s received unusable detections", self.source_id)
            return self._fallback(boxes)

        present = {int(value) for value in class_ids.tolist()}
        accepted: dict[int, str] = {}
        for class_id in sorted(set(self._trackers) | present):
            tracker = self._trackers.get(class_id)
            if tracker is None:
                tracker = self._new_tracker()
                self._trackers[class_id] = tracker
            indices = np.flatnonzero(class_ids == class_id)
            subset = detections[indices]
            try:
                tracker.update(subset)
            except Exception:
                LOG.exception(
                    "ByteTrack update failed source=%s class_id=%s",
                    self.source_id,
                    class_id,
                )
                return self._fallback(boxes)
            for track in tracker.tracked_stracks:
                if int(track.frame_id) != int(tracker.frame_id):
                    continue
                subset_index = int(track.idx)
                if subset_index < 0 or subset_index >= len(indices):
                    continue
                token = self._tokens.get(track)
                if token is None:
                    token = (
                        f"{self.source_id}:{self._session_id}:"
                        f"{self._next_token}"
                    )
                    self._next_token += 1
                    self._tokens[track] = token
                accepted[int(indices[subset_index])] = token

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
