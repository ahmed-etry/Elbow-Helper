from __future__ import annotations

import json
import logging
import re
from typing import Any

from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic
from .config import STATE_FILE

LOGGER = logging.getLogger(__name__)
FALLBACK_THREADS_KEY = "_fallback_threads"
FALLBACK_INFO_MESSAGE_KEY = "_fallback_info_message_id"


class HibernationStateReader:
    """Read-only member state exposed to other features."""

    def get_member(self, member_id: int) -> dict[str, Any] | None:
        entry = load_hibernation_state().get(str(member_id))
        return dict(entry) if isinstance(entry, dict) else None


def load_hibernation_state() -> dict[str, Any]:
    try:
        payload = read_json(STATE_FILE)
        if isinstance(payload, dict):
            return payload
        return {}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}
    except OSError as exc:
        LOGGER.warning("Failed loading %s: %s", STATE_FILE, exc)
        return {}


def save_hibernation_state(data: dict[str, Any]) -> None:
    try:
        write_json_atomic(STATE_FILE, data, indent=2)
    except (OSError, TypeError) as exc:
        LOGGER.error("Failed saving %s: %s", STATE_FILE, exc)


def get_fallback_info_message_id(data: dict[str, Any]) -> int | None:
    raw = data.get(FALLBACK_INFO_MESSAGE_KEY)
    return raw if isinstance(raw, int) else None


def set_fallback_info_message_id(data: dict[str, Any], message_id: int | None) -> None:
    data[FALLBACK_INFO_MESSAGE_KEY] = message_id


def get_fallback_threads(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get(FALLBACK_THREADS_KEY)
    if isinstance(raw, dict):
        return raw
    data[FALLBACK_THREADS_KEY] = {}
    return data[FALLBACK_THREADS_KEY]


def get_fallback_thread_entry(data: dict[str, Any], user_id: int) -> dict[str, Any] | None:
    entry = get_fallback_threads(data).get(str(user_id))
    if isinstance(entry, dict):
        return entry
    return None


def set_fallback_thread_entry(data: dict[str, Any], user_id: int, entry: dict[str, Any]) -> None:
    get_fallback_threads(data)[str(user_id)] = entry


def remove_fallback_thread_entry(data: dict[str, Any], user_id: int) -> dict[str, Any] | None:
    entry = get_fallback_threads(data).pop(str(user_id), None)
    if isinstance(entry, dict):
        return entry
    return None


def extract_owner_id_from_topic(topic: str | None) -> int | None:
    if not topic:
        return None
    mention_match = re.search(r"<@!?(\d+)>", topic)
    if mention_match:
        return int(mention_match.group(1))
    raw = topic.strip()
    if raw.isdigit():
        return int(raw)
    return None
