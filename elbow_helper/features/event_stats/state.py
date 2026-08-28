"""Disk-backed state helpers for event stats."""

from __future__ import annotations

import json
import logging
from typing import Any

from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic
from elbow_helper.domain.timezones import canonical_timezone_name
from .config import DEFAULT_GRACE_HOURS
from .config import MAX_GRACE_HOURS
from .config import MAX_EVENT_NAME_LENGTH
from .config import STATE_FILE
from .config import STATE_SCHEMA_VERSION
from .config import build_default_state_events
from .config import get_preset_definition

LOGGER = logging.getLogger(__name__)


def _default_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "events": build_default_state_events(),
    }


def _coerce_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return default


def _coerce_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_name(value: Any, fallback: str) -> str:
    name = str(value or "").strip()
    return name[:MAX_EVENT_NAME_LENGTH] if name else fallback


def _coerce_grace_hours(value: Any, fallback: int) -> int:
    hours = _coerce_int(value)
    if hours is None:
        return fallback
    return max(0, min(hours, MAX_GRACE_HOURS))


def _coerce_timezone(value: Any) -> str:
    raw_value = str(value or "UTC").strip() or "UTC"
    canonical = canonical_timezone_name(raw_value)
    if canonical is None:
        return "UTC"
    return canonical


def _normalize_preset_entry(raw: dict[str, Any], fallback_position: int) -> dict[str, Any] | None:
    key = str(raw.get("key") or "").strip()
    preset = get_preset_definition(key)
    if preset is None:
        return None
    raw_name = str(raw.get("name") or "").strip()
    preset_name = str(preset["name"])
    return {
        "key": key,
        "source": "preset",
        "enabled": _coerce_bool(raw.get("enabled"), True),
        "name": _coerce_name(raw_name, preset_name),
        "grace_period_hours": _coerce_grace_hours(raw.get("grace_period_hours"), int(preset["grace_period_hours"])),
        "category_id": _coerce_int(raw.get("category_id")),
        "channel_id": _coerce_int(raw.get("channel_id")),
        "position": _coerce_int(raw.get("position")) if _coerce_int(raw.get("position")) is not None else fallback_position,
    }


def _normalize_custom_entry(raw: dict[str, Any], fallback_position: int) -> dict[str, Any] | None:
    key = str(raw.get("key") or "").strip()
    start = str(raw.get("start") or "").strip()
    end = str(raw.get("end") or "").strip()
    if not key or not start or not end:
        return None
    return {
        "key": key,
        "source": "custom",
        "type": "one-time",
        "enabled": _coerce_bool(raw.get("enabled"), True),
        "name": _coerce_name(raw.get("name"), "Event"),
        "start": start,
        "end": end,
        "timezone": _coerce_timezone(raw.get("timezone")),
        "grace_period_hours": _coerce_grace_hours(raw.get("grace_period_hours"), DEFAULT_GRACE_HOURS),
        "category_id": _coerce_int(raw.get("category_id")),
        "channel_id": _coerce_int(raw.get("channel_id")),
        "position": _coerce_int(raw.get("position")) if _coerce_int(raw.get("position")) is not None else fallback_position,
    }


def normalize_state(raw_state: Any) -> dict[str, Any]:
    if not isinstance(raw_state, dict):
        return _default_state()

    normalized_events: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    preset_defaults = build_default_state_events()
    raw_events = raw_state.get("events")
    if not isinstance(raw_events, list):
        raw_events = []

    for index, raw_event in enumerate(raw_events):
        if not isinstance(raw_event, dict):
            continue
        key = str(raw_event.get("key") or "").strip()
        if not key:
            continue
        if key in seen_keys:
            LOGGER.warning("Skipping duplicate event key in %s: %s", STATE_FILE, key)
            continue

        if get_preset_definition(key) is not None or str(raw_event.get("source") or "").strip().lower() == "preset":
            entry = _normalize_preset_entry(raw_event, fallback_position=index)
        else:
            entry = _normalize_custom_entry(raw_event, fallback_position=len(preset_defaults) + index)

        if entry is None:
            LOGGER.warning("Skipping invalid event state in %s: %s", STATE_FILE, raw_event)
            continue

        normalized_events.append(entry)
        seen_keys.add(entry["key"])

    for default_event in preset_defaults:
        if default_event["key"] not in seen_keys:
            normalized_events.append(default_event)
            seen_keys.add(default_event["key"])

    normalized_events.sort(key=lambda item: (int(item.get("position", 0)), item["key"]))
    for position, event in enumerate(normalized_events):
        event["position"] = position

    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "events": normalized_events,
    }


def ensure_state() -> dict[str, Any]:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        state = _default_state()
        try:
            write_json_atomic(STATE_FILE, state, indent=2)
        except OSError as exc:
            LOGGER.warning("Failed creating %s: %s", STATE_FILE, exc)
        return state

    try:
        raw_state = read_json(STATE_FILE)
    except (json.JSONDecodeError, OSError) as exc:
        LOGGER.warning("Failed loading %s: %s", STATE_FILE, exc)
        return _default_state()

    state = normalize_state(raw_state)
    if state != raw_state:
        save_state(state)
    return state


def save_state(state: dict[str, Any]) -> None:
    normalized = normalize_state(state)
    state.clear()
    state.update(normalized)
    try:
        write_json_atomic(STATE_FILE, state, indent=2)
    except (OSError, TypeError) as exc:
        LOGGER.error("Failed saving %s: %s", STATE_FILE, exc)
