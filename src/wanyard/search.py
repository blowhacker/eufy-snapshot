"""Constrained natural-language planning for evidence search.

The planner is intentionally deterministic.  It turns common questions into a
small, auditable query plan; a future LLM/VLM may produce the same schema, but
must not get direct database access or invent evidence.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, time as datetime_time, timedelta


_OBJECT_ALIASES: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    (("people", "person", "someone", "somebody", "human"), ("person",), "person"),
    (("cars", "car", "vehicle", "vehicles"), ("car", "truck", "bus", "motorcycle"), "vehicle"),
    (("dogs", "dog"), ("dog",), "dog"),
    (("cats", "cat"), ("cat",), "cat"),
    (("birds", "bird"), ("bird",), "bird"),
    (("foxes", "fox"), ("cat", "dog"), "fox"),
)
_SCENE_TERMS = {
    "bin": "bins",
    "bins": "bins",
    "rubbish bin": "bins",
    "trash can": "bins",
    "gate": "gate",
    "door": "door",
}
_VISUAL_ATTRIBUTES = (
    "black", "white", "red", "orange", "grey", "gray", "brown",
    "striped", "small", "large",
)


@dataclass(frozen=True)
class SearchPlan:
    query: str
    intent: str
    subject: str
    classes: tuple[str, ...]
    source_ids: tuple[str, ...]
    since: float
    until: float
    time_label: str
    visual_requirements: tuple[str, ...]
    evidence_level: str

    def payload(self) -> dict:
        return asdict(self)


def plan_search(
    query: str,
    sources: list[dict],
    *,
    now: datetime | None = None,
) -> SearchPlan:
    """Parse a bounded set of useful camera-history questions.

    ``now`` should be timezone-aware in production. Tests may supply a naive
    value; all date arithmetic remains in that same local clock.
    """
    clean = " ".join(str(query).strip().split())
    if not clean:
        raise ValueError("ask a question")
    if len(clean) > 300:
        raise ValueError("question must be 300 characters or fewer")
    lowered = clean.casefold()
    reference = now or datetime.now().astimezone()
    since, until, time_label = _time_window(lowered, reference)
    source_ids = _matching_sources(lowered, sources)
    subject, classes = _matching_subject(lowered)

    scene_subject = next(
        (label for term, label in _SCENE_TERMS.items() if _has_term(lowered, term)),
        None,
    )
    moved = bool(re.search(r"\b(move|moved|moving|change|changed)\b", lowered))
    intent = "scene_change" if scene_subject and moved else (
        "when" if re.search(r"\b(when|what time|which time)\b", lowered) else "find"
    )
    visual = [
        attribute for attribute in _VISUAL_ATTRIBUTES
        if _has_term(lowered, attribute)
    ]
    evidence_level = "detector"
    if scene_subject:
        subject = scene_subject
    if intent == "scene_change":
        classes = ()
        visual.append("scene state over time")
        evidence_level = "scene-index-required"
    elif subject == "fox":
        visual.append("species")
        evidence_level = "visual-verification-required"
    elif visual:
        evidence_level = "visual-verification-required"

    return SearchPlan(
        query=clean,
        intent=intent,
        subject=subject,
        classes=classes,
        source_ids=source_ids,
        since=since.timestamp(),
        until=until.timestamp(),
        time_label=time_label,
        visual_requirements=tuple(dict.fromkeys(visual)),
        evidence_level=evidence_level,
    )


def summarize_search(plan: SearchPlan, events: list[dict]) -> tuple[str, str]:
    """Return a concise answer and an explicit confidence label."""
    count = len(events)
    if plan.intent == "scene_change":
        return (
            f"I can't verify when the {plan.subject} moved yet. "
            "Detections do not record fixed-object position; this needs the "
            "scene-state index planned for the next checkpoint.",
            "not indexed",
        )
    noun = plan.subject if plan.subject != "anything" else "matching event"
    if not events:
        qualifier = (
            " among the detector candidates"
            if plan.visual_requirements else ""
        )
        return (
            f"I found no {noun} evidence{qualifier} {plan.time_label}.",
            "no match",
        )
    suffix = "s" if count != 1 else ""
    if plan.subject == "fox":
        return (
            f"I found {count} cat-or-dog detector candidate{suffix} "
            f"{plan.time_label}. A visual model must inspect them before I can "
            "call any of them a fox.",
            "candidates",
        )
    if plan.visual_requirements:
        attrs = ", ".join(plan.visual_requirements)
        return (
            f"I found {count} {noun} detector candidate{suffix} {plan.time_label}, "
            f"but {attrs} has not been visually verified yet.",
            "candidates",
        )
    return (
        f"I found {count} {noun} detection{suffix} {plan.time_label}.",
        "detector evidence",
    )


def _matching_sources(query: str, sources: list[dict]) -> tuple[str, ...]:
    matches = []
    for source in sources:
        source_id = str(source.get("id") or "")
        source_name = str(source.get("name") or source_id)
        if (
            source_id and _has_term(query, source_id.casefold())
        ) or (
            source_name and _has_term(query, source_name.casefold())
        ):
            matches.append(source_id)
    return tuple(matches)


def _matching_subject(query: str) -> tuple[str, tuple[str, ...]]:
    for aliases, classes, subject in _OBJECT_ALIASES:
        if any(_has_term(query, alias) for alias in aliases):
            return subject, classes
    return "anything", ()


def _time_window(query: str, now: datetime) -> tuple[datetime, datetime, str]:
    today = datetime.combine(now.date(), datetime_time.min, tzinfo=now.tzinfo)
    if _has_term(query, "yesterday"):
        return today - timedelta(days=1), today, "yesterday"
    if _has_term(query, "today"):
        return today, now, "today"
    if _has_term(query, "this morning"):
        noon = today + timedelta(hours=12)
        return today, min(now, noon), "this morning"
    recent = re.search(r"\b(?:last|past)\s+(\d{1,3})\s+(hour|hours|day|days)\b", query)
    if recent:
        amount = min(365, int(recent.group(1)))
        unit = recent.group(2)
        delta = timedelta(hours=amount) if unit.startswith("hour") else timedelta(days=amount)
        return now - delta, now, f"in the last {amount} {unit}"
    if _has_term(query, "last night"):
        start = today - timedelta(hours=6)
        end = min(now, today + timedelta(hours=6))
        return start, end, "last night"
    iso_date = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", query)
    if iso_date:
        try:
            date_start = datetime.combine(
                datetime.strptime(iso_date.group(0), "%Y-%m-%d").date(),
                datetime_time.min,
                tzinfo=now.tzinfo,
            )
        except ValueError:
            pass
        else:
            return date_start, date_start + timedelta(days=1), f"on {iso_date.group(0)}"
    return now - timedelta(hours=24), now, "in the last 24 hours"


def _has_term(text: str, term: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None
