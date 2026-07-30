from __future__ import annotations

from dataclasses import dataclass
from email.header import Header
import hashlib
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urljoin, urlparse


LOG = logging.getLogger(__name__)

_TOPIC_RE = re.compile(r"[-_A-Za-z0-9]{1,64}")
_DEFAULT_SERVER = "https://ntfy.sh"
_SETTING_PREFIX = "ntfy_"
_MAX_BATCH = 20
_MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024


class NtfyPublishError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return (
            self.status_code is None
            or self.status_code == 429
            or self.status_code >= 500
        )


@dataclass(frozen=True)
class NtfyConfig:
    enabled: bool
    server: str
    topic: str
    token: str
    include_thumbnail: bool
    base_url: str

    @property
    def destination_key(self) -> str:
        raw = f"{self.server}\0{self.topic}".encode()
        return hashlib.sha256(raw).hexdigest()[:24]

    def public_payload(self, status: dict | None = None) -> dict:
        return {
            "enabled": self.enabled,
            "server": self.server,
            "topic": self.topic,
            "token_set": bool(self.token),
            "include_thumbnail": self.include_thumbnail,
            "base_url": self.base_url,
            "status": status or {},
        }


def _setting(video_db, name: str, default=None):
    return video_db.get_setting(f"{_SETTING_PREFIX}{name}", default)


def _set_setting(video_db, name: str, value) -> None:
    video_db.set_setting(f"{_SETTING_PREFIX}{name}", value)


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _new_topic() -> str:
    return f"wanyard-{secrets.token_hex(12)}"


def _normalize_url(raw, *, field: str, allow_blank: bool = False) -> str:
    value = str(raw or "").strip()
    if not value and allow_blank:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field} must be an http or https URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{field} must not include credentials, a query, or a fragment")
    return value.rstrip("/")


def load_ntfy_config(
    video_db,
    *,
    default_base_url: str = "",
    ensure_topic: bool = True,
) -> NtfyConfig:
    topic = str(_setting(video_db, "topic", "") or "").strip()
    if not topic and ensure_topic:
        topic = _new_topic()
        _set_setting(video_db, "topic", topic)
    server = str(_setting(video_db, "server", _DEFAULT_SERVER) or _DEFAULT_SERVER)
    base_url = str(_setting(video_db, "base_url", "") or "").strip()
    if not base_url and default_base_url:
        base_url = default_base_url.rstrip("/")
    return NtfyConfig(
        enabled=_as_bool(_setting(video_db, "enabled", "0")),
        server=server.rstrip("/"),
        topic=topic,
        token=str(_setting(video_db, "token", "") or ""),
        include_thumbnail=_as_bool(
            _setting(video_db, "include_thumbnail", "1"), True
        ),
        base_url=base_url.rstrip("/"),
    )


def save_ntfy_config(
    video_db,
    data: dict,
    *,
    default_base_url: str = "",
) -> NtfyConfig:
    if not isinstance(data, dict):
        raise ValueError("settings must be an object")
    current = load_ntfy_config(
        video_db, default_base_url=default_base_url, ensure_topic=True
    )
    topic = str(data.get("topic", current.topic) or "").strip()
    if not _TOPIC_RE.fullmatch(topic):
        raise ValueError(
            "topic must be 1-64 letters, numbers, dashes, or underscores"
        )
    server = _normalize_url(
        data.get("server", current.server), field="ntfy server"
    )
    base_url = _normalize_url(
        data.get("base_url", current.base_url or default_base_url),
        field="Wanyard URL",
        allow_blank=True,
    )
    token = current.token
    if data.get("clear_token"):
        token = ""
    elif "token" in data and str(data.get("token") or ""):
        token = str(data["token"]).strip()
    enabled = _as_bool(data.get("enabled", current.enabled))
    include_thumbnail = _as_bool(
        data.get("include_thumbnail", current.include_thumbnail), True
    )

    for name, value in (
        ("enabled", "1" if enabled else "0"),
        ("server", server),
        ("topic", topic),
        ("token", token),
        ("include_thumbnail", "1" if include_thumbnail else "0"),
        ("base_url", base_url),
    ):
        _set_setting(video_db, name, value)

    saved = NtfyConfig(
        enabled=enabled,
        server=server,
        topic=topic,
        token=token,
        include_thumbnail=include_thumbnail,
        base_url=base_url,
    )
    video_db.configure_notification_delivery(
        "ntfy",
        saved.destination_key,
        force_seed=enabled and not current.enabled,
    )
    return saved


def _absolute_url(base_url: str, value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    if not base_url:
        return None
    return urljoin(f"{base_url.rstrip('/')}/", value.lstrip("/"))


def _class_tags(cls: str) -> list[str]:
    mapped = {
        "person": "bust_in_silhouette",
        "bird": "bird",
        "cat": "cat2",
        "dog": "dog",
        "car": "blue_car",
        "bus": "bus",
        "truck": "truck",
        "bicycle": "bike",
        "motorcycle": "motorcycle",
    }
    return [mapped.get(str(cls or "").lower(), "eyes")]


def _header_value(value, *, limit: int) -> str:
    clean = " ".join(str(value or "").splitlines()).strip()[:limit]
    try:
        clean.encode("ascii")
    except UnicodeEncodeError:
        return Header(clean, "utf-8").encode()
    return clean


def _thumbnail_bytes(
    config: NtfyConfig,
    notification: dict,
    *,
    timeout: float,
    opener,
) -> bytes | None:
    embedded = notification.get("_thumb_jpeg")
    if embedded:
        data = bytes(embedded)
        return data if len(data) <= _MAX_THUMBNAIL_BYTES else None

    url = _absolute_url(config.base_url, notification.get("thumb_url"))
    if not url:
        return None
    request = urllib.request.Request(
        url,
        headers={"Accept": "image/jpeg,image/*;q=0.9"},
        method="GET",
    )
    try:
        with opener(request, timeout=max(0.5, float(timeout))) as response:
            status = int(getattr(response, "status", 200))
            data = response.read(_MAX_THUMBNAIL_BYTES + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        LOG.warning("could not load ntfy thumbnail %s: %s", url, exc)
        return None
    if status < 200 or status >= 300:
        LOG.warning("could not load ntfy thumbnail %s: HTTP %s", url, status)
        return None
    if not data or len(data) > _MAX_THUMBNAIL_BYTES:
        LOG.warning("ntfy thumbnail %s is empty or exceeds 2 MB", url)
        return None
    return data


def publish_ntfy(
    config: NtfyConfig,
    notification: dict,
    *,
    timeout: float = 8.0,
    opener=urllib.request.urlopen,
) -> str | None:
    click = _absolute_url(config.base_url, notification.get("target_url"))
    title = str(notification.get("title") or "Wanyard")
    message = str(notification.get("body") or "Detection")
    tags = _class_tags(str(notification.get("class") or ""))
    thumbnail = None
    if config.include_thumbnail:
        thumbnail = _thumbnail_bytes(
            config, notification, timeout=timeout, opener=opener
        )

    if thumbnail:
        filename = (
            f"wanyard-{notification.get('class') or 'detection'}-"
            f"{int(float(notification.get('event_ts') or time.time()))}.jpg"
        )
        headers = {
            "Content-Type": "image/jpeg",
            "X-Title": _header_value(title, limit=256),
            "X-Message": _header_value(message, limit=2048),
            "X-Tags": ",".join(tags),
            "X-Priority": "3",
            "X-Filename": filename,
        }
        if click:
            headers["X-Click"] = click
        request = urllib.request.Request(
            f"{config.server.rstrip('/')}/{quote(config.topic, safe='')}",
            data=thumbnail,
            headers=headers,
            method="PUT",
        )
    else:
        payload = {
            "topic": config.topic,
            "title": title,
            "message": message,
            "tags": tags,
            "priority": 3,
        }
        if click:
            payload["click"] = click
        request = urllib.request.Request(
            f"{config.server.rstrip('/')}/",
            data=json.dumps(payload, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    if config.token:
        request.add_header("Authorization", f"Bearer {config.token}")
    try:
        with opener(request, timeout=max(0.5, float(timeout))) as response:
            raw = response.read()
            status = int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        retry_after = None
        try:
            retry_after = float(exc.headers.get("Retry-After", ""))
        except (TypeError, ValueError):
            pass
        detail = exc.read().decode(errors="replace")[:300]
        raise NtfyPublishError(
            detail or f"ntfy returned HTTP {exc.code}",
            status_code=int(exc.code),
            retry_after=retry_after,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NtfyPublishError(str(exc)) from exc
    if status < 200 or status >= 300:
        raise NtfyPublishError(
            f"ntfy returned HTTP {status}", status_code=status
        )
    try:
        result = json.loads(raw.decode()) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        result = {}
    return str(result.get("id")) if result.get("id") else None


def send_ntfy_test(config: NtfyConfig, *, opener=urllib.request.urlopen) -> str | None:
    return publish_ntfy(
        config,
        {
            "title": "Wanyard test",
            "body": "Notifications are connected. Commence camera dance.",
            "class": "person",
            "event_ts": time.time(),
            "target_url": "/settings#notifications",
            # A test verifies the channel. The next real detection verifies the
            # class-correct thumbnail path.
            "thumb_url": None,
        },
        opener=opener,
    )


def dispatch_ntfy_notifications(
    video_db,
    *,
    now: float | None = None,
    publisher=publish_ntfy,
) -> dict:
    now = time.time() if now is None else float(now)
    config = load_ntfy_config(video_db, ensure_topic=False)
    if not config.enabled or not config.topic:
        return {"queued": 0, "delivered": 0, "failed": 0}

    video_db.configure_notification_delivery("ntfy", config.destination_key)
    max_age = max(
        0.0,
        float(os.environ.get("WANYARD_NTFY_MAX_EVENT_AGE_SECONDS", "300")),
    )
    queued = video_db.enqueue_notification_deliveries(
        "ntfy",
        config.destination_key,
        now=now,
        max_event_age=max_age,
        limit=200,
    )
    deliveries = video_db.pending_notification_deliveries(
        "ntfy", config.destination_key, now=now, limit=_MAX_BATCH
    )
    delivered = failed = 0
    for delivery in deliveries:
        try:
            if config.include_thumbnail:
                delivery["_thumb_jpeg"] = video_db.get_notification_thumb(
                    int(delivery["id"])
                )
            remote_id = publisher(config, delivery)
        except NtfyPublishError as exc:
            video_db.fail_notification_delivery(
                int(delivery["_delivery_id"]),
                str(exc),
                retryable=exc.retryable,
                retry_after=exc.retry_after,
                now=now,
            )
            failed += 1
        except Exception as exc:
            LOG.exception("unexpected ntfy publish failure")
            video_db.fail_notification_delivery(
                int(delivery["_delivery_id"]),
                f"{type(exc).__name__}: {exc}",
                retryable=True,
                now=now,
            )
            failed += 1
        else:
            video_db.complete_notification_delivery(
                int(delivery["_delivery_id"]), remote_id, now=now
            )
            delivered += 1
    return {"queued": queued, "delivered": delivered, "failed": failed}
