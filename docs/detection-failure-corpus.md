# Detection Failure Corpus

This file tracks detection failures we want to preserve as regression cases while
we improve notifications, zone matching, and object permanence. Keep cases here
until they are represented by automated tests or a stronger offline evaluation
fixture.

Status values:

- `open`: still needs diagnosis or a fix.
- `mitigated`: current code appears to handle it, but it is not yet a durable
  automated regression test.
- `needs-repro`: needs a fresh sample or UI reproduction.
- `closed`: covered by a durable test or intentionally accepted behavior.

## Cases

| ID | Status | Type | Link / timestamp | Expected | Observed | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| FP-001 | mitigated | False notification | `tapo-garden`, `ts=1780605347`, `cls=person`, `zone=2` | No person notification. | Low-confidence person false positive in back yard. | High-res YOLO confirmation rejected this sample in the manual banana test set. |
| FP-002 | mitigated | False notification | `tapo-garden`, `ts=1780604732`, `cls=person`, `zone=2` | No person notification. | Back-yard person notification was shown. | High-res YOLO confirmation rejected the matching cached candidate. |
| FP-003 | mitigated | False notification | `tapo-front`, `ts=1780606889`, `cls=person`, `zone=3` | No person notification. | Front person notification was shown. | High-res YOLO confirmation rejected the matching cached candidate. |
| FP-004 | mitigated | False notification | `tapo-front`, `ts=1780614741`, `cls=person`, `zone=3` | No person notification. | HLS person notification was shown. | High-res YOLO confirmation rejected the matching cached candidate. |
| FP-005 | mitigated | False detection | `tapo-garden`, `ts=1780035525`, `cls=person`, `zone=none` | No person detection. | Person false positive. | Rejected by high-res YOLO confirmation in the manual test set. |
| FP-006 | mitigated | False detection | `tapo-garden`, `ts=1780046658`, `cls=person`, `zone=none` | No person detection. | Person false positive. | Rejected by high-res YOLO confirmation in the manual test set. |
| FP-007 | mitigated | False detection | `tapo-front`, `ts=1780068538`, `cls=suitcase`, `zone=none` | No suitcase detection. | Suitcase false positive. | Rejected by high-res YOLO confirmation in the manual test set. |
| FP-008 | mitigated | False detection | `tapo-front`, `ts=1780121904`, `cls=suitcase`, `zone=none` | No suitcase detection. | Suitcase false positive. | Rejected by high-res YOLO confirmation in the manual test set. |
| FP-009 | mitigated | False detection | `tapo-garden`, `ts=1780448071`, `cls=bird`, `zone=none` | No bird detection. | Bird false positive. | Rejected by high-res YOLO confirmation in the manual test set. |
| FP-010 | mitigated | False detection | `tapo-garden`, `ts=1780048494`, `cls=bird`, `zone=none` | No bird detection. | Bird false positive. | Rejected by high-res YOLO confirmation in the manual test set. |
| FP-011 | mitigated | False detection | `tapo-garden`, `ts=1780378938`, `cls=bird`, `zone=none` | No bird detection. | Bird false positive. | Rejected by high-res YOLO confirmation in the manual test set. |
| ZON-001 | open | Missed zone inclusion | `tapo-garden`, `ts=1780610224`, `cls=person`, `zone=none` | Person should appear when filtering to back yard. | Legit person is not shown under the back-yard zone filter. | The box center is inside the zone in later frames. This points at event representative frame selection and tracking, not just simple zone geometry. |
| TRK-001 | open | Object permanence split | `tapo-garden`, around `ts=1780610224` | Fast approaching person should remain one useful event window. | Raw detections exist, but the surfaced event can be missing, split, or anchored poorly. | A fast-moving intruder can be the most important case, so this needs a tracking/event-window fix rather than class-specific rules. |
| TS-001 | open | Notification timestamp UX | Latest back-yard notification on banana, notification `id=8`, event `h:646717`, `tapo-garden`, `event_ts=1780677716.571` | Notification should seek to the moment a user expects to see the person. | User reported the timestamp seems off. | Initial DB check: raw person detections span roughly `1780677713.3` onward, and the notification event timestamp lands within the detection run. `created_at` was about 91 seconds later, so the event timestamp is not obviously wrong, but the link may need pre-roll or first-seen anchoring. |

## Current Evidence

- High-res YOLO confirmation rejected all known false-positive samples above in
  the manual banana test run.
- The confirmation path uses the existing YOLO server/model and serializes
  predictions through its predict lock, so it does not load a second CUDA model.
- The current zone failure is not solved by a class-specific rule. It needs
  class-agnostic event evidence across frames.
- A class-agnostic raw-frame zone test using `center-in-zone OR overlap >= 20%`
  with at least two supporting frames preserved the true positive while avoiding
  the tested old false notification cases.

## Candidate Fixes To Evaluate

- Replace single representative-frame zone decisions with a short event-window
  decision over raw detections.
- Improve object permanence by linking adjacent detections with motion-tolerant
  geometry, not only same-frame box overlap.
- Anchor notification links to first useful evidence or add a small pre-roll,
  especially for HLS events.
- Promote this corpus into a repeatable offline evaluation script once the
  failure list stabilizes.
