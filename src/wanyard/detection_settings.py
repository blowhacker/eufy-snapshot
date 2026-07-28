"""Configurable YOLO/COCO class selection for the detection pipeline."""

from __future__ import annotations

import json
import threading
import time

DETECTION_CLASSES_SETTING = "detection_classes"

# Ultralytics YOLO11 COCO class IDs. Keep this catalog complete so Settings can
# expose every class the bundled model is capable of returning.
COCO_CLASSES: dict[int, str] = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    4: "airplane",
    5: "bus",
    6: "train",
    7: "truck",
    8: "boat",
    9: "traffic light",
    10: "fire hydrant",
    11: "stop sign",
    12: "parking meter",
    13: "bench",
    14: "bird",
    15: "cat",
    16: "dog",
    17: "horse",
    18: "sheep",
    19: "cow",
    20: "elephant",
    21: "bear",
    22: "zebra",
    23: "giraffe",
    24: "backpack",
    25: "umbrella",
    26: "handbag",
    27: "tie",
    28: "suitcase",
    29: "frisbee",
    30: "skis",
    31: "snowboard",
    32: "sports ball",
    33: "kite",
    34: "baseball bat",
    35: "baseball glove",
    36: "skateboard",
    37: "surfboard",
    38: "tennis racket",
    39: "bottle",
    40: "wine glass",
    41: "cup",
    42: "fork",
    43: "knife",
    44: "spoon",
    45: "bowl",
    46: "banana",
    47: "apple",
    48: "sandwich",
    49: "orange",
    50: "broccoli",
    51: "carrot",
    52: "hot dog",
    53: "pizza",
    54: "donut",
    55: "cake",
    56: "chair",
    57: "couch",
    58: "potted plant",
    59: "bed",
    60: "dining table",
    61: "toilet",
    62: "tv",
    63: "laptop",
    64: "mouse",
    65: "remote",
    66: "keyboard",
    67: "cell phone",
    68: "microwave",
    69: "oven",
    70: "toaster",
    71: "sink",
    72: "refrigerator",
    73: "book",
    74: "clock",
    75: "vase",
    76: "scissors",
    77: "teddy bear",
    78: "hair drier",
    79: "toothbrush",
}
COCO_CLASS_IDS = {name: class_id for class_id, name in COCO_CLASSES.items()}

# Useful home/CCTV classes, plus competing labels that prevent common
# closed-set mistakes: airplanes and kites becoming birds, and plush toys
# becoming people. Existing installations with no saved setting adopt these.
DEFAULT_DETECTION_CLASSES: tuple[str, ...] = (
    "person",
    "bicycle",
    "car",
    "motorcycle",
    "airplane",
    "bus",
    "truck",
    "bird",
    "cat",
    "dog",
    "backpack",
    "suitcase",
    "kite",
    "teddy bear",
)

CLASS_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "People & transport",
        (
            "person", "bicycle", "car", "motorcycle", "airplane", "bus",
            "train", "truck", "boat",
        ),
    ),
    (
        "Animals",
        (
            "bird", "cat", "dog", "horse", "sheep", "cow", "elephant",
            "bear", "zebra", "giraffe",
        ),
    ),
    (
        "Street & belongings",
        (
            "traffic light", "fire hydrant", "stop sign", "parking meter",
            "bench", "backpack", "umbrella", "handbag", "tie", "suitcase",
        ),
    ),
    (
        "Sports",
        (
            "frisbee", "skis", "snowboard", "sports ball", "kite",
            "baseball bat", "baseball glove", "skateboard", "surfboard",
            "tennis racket",
        ),
    ),
    (
        "Food & tableware",
        (
            "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl",
            "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
            "hot dog", "pizza", "donut", "cake",
        ),
    ),
    (
        "Home & electronics",
        (
            "chair", "couch", "potted plant", "bed", "dining table", "toilet",
            "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
            "microwave", "oven", "toaster", "sink", "refrigerator", "book",
            "clock", "vase", "scissors", "teddy bear", "hair drier",
            "toothbrush",
        ),
    ),
)


def _ordered(names) -> list[str]:
    wanted = set(names)
    return [name for name in COCO_CLASSES.values() if name in wanted]


def normalize_detection_classes(value) -> list[str]:
    """Read a stored value; invalid/corrupt settings safely use defaults."""
    if value is None:
        return list(DEFAULT_DETECTION_CLASSES)
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, (list, tuple)):
        return list(DEFAULT_DETECTION_CLASSES)
    names = [
        str(name).strip().lower()
        for name in raw
        if str(name).strip().lower() in COCO_CLASS_IDS
    ]
    return _ordered(names) or list(DEFAULT_DETECTION_CLASSES)


def validate_detection_classes(value) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("classes must be an array")
    names = [str(name).strip().lower() for name in value]
    unknown = sorted({name for name in names if name not in COCO_CLASS_IDS})
    if unknown:
        raise ValueError(f"unsupported detection class: {unknown[0]}")
    selected = _ordered(names)
    if not selected:
        raise ValueError("select at least one detection class")
    return selected


def configured_detection_classes(video_db) -> list[str]:
    return normalize_detection_classes(
        video_db.get_setting(DETECTION_CLASSES_SETTING)
    )


def configured_detection_class_ids(video_db) -> list[int]:
    return [
        COCO_CLASS_IDS[name]
        for name in configured_detection_classes(video_db)
    ]


def save_detection_classes(video_db, names) -> list[str]:
    selected = validate_detection_classes(names)
    video_db.set_setting(
        DETECTION_CLASSES_SETTING,
        json.dumps(selected, separators=(",", ":")),
    )
    return selected


def detection_settings_payload(video_db) -> dict:
    enabled = configured_detection_classes(video_db)
    defaults = set(DEFAULT_DETECTION_CLASSES)
    return {
        "enabled": enabled,
        "defaults": list(DEFAULT_DETECTION_CLASSES),
        "groups": [
            {
                "name": group_name,
                "classes": [
                    {
                        "id": COCO_CLASS_IDS[name],
                        "name": name,
                        "default_enabled": name in defaults,
                    }
                    for name in names
                ],
            }
            for group_name, names in CLASS_GROUPS
        ],
    }


class DetectionClassSelector:
    """Small cross-thread cache for the detector's shared SQLite setting."""

    def __init__(self, video_db, ttl_seconds: float = 2.0) -> None:
        self.video_db = video_db
        self.ttl_seconds = max(0.1, float(ttl_seconds))
        self._lock = threading.Lock()
        self._expires_at = 0.0
        self._ids: tuple[int, ...] = ()

    def ids(self) -> list[int]:
        now = time.monotonic()
        with self._lock:
            if now >= self._expires_at or not self._ids:
                self._ids = tuple(configured_detection_class_ids(self.video_db))
                self._expires_at = now + self.ttl_seconds
            return list(self._ids)
