"""Disk-backed state helpers for clan transfers."""

from __future__ import annotations

import json
import logging
from typing import Any

from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic
from .config import CLAN_TRANSFER_QUEUES
from .config import CLAN_TRANSFER_STATE_FILE

LOGGER = logging.getLogger(__name__)


def default_clan_state(clan_code: str) -> dict[str, Any]:
    queue = CLAN_TRANSFER_QUEUES[clan_code]
    return {
        "thread_id": queue["thread_id"],
        "role_id": queue["role_id"],
        "queue_message_id": None,
        "pending": [],
        "last_ping_message_id": None,
    }


def build_default_state() -> dict[str, Any]:
    return {
        "global_board_message_id": None,
        "clans": {code: default_clan_state(code) for code in CLAN_TRANSFER_QUEUES},
    }


def load_state() -> dict[str, Any]:
    if not CLAN_TRANSFER_STATE_FILE.exists():
        return build_default_state()

    try:
        data = read_json(CLAN_TRANSFER_STATE_FILE)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        LOGGER.warning("Failed to load %s: %s", CLAN_TRANSFER_STATE_FILE, exc)
        return build_default_state()

    if not isinstance(data, dict):
        return build_default_state()

    data.setdefault("global_board_message_id", None)
    data.setdefault("clans", {})
    for code in CLAN_TRANSFER_QUEUES:
        existing = data["clans"].get(code, {})
        merged = {**default_clan_state(code), **existing}
        merged.setdefault("pending", [])
        data["clans"][code] = merged
    return data


def save_state(data: dict[str, Any]) -> None:
    try:
        write_json_atomic(CLAN_TRANSFER_STATE_FILE, data, indent=2)
    except (OSError, TypeError) as exc:
        LOGGER.error("Failed to save %s: %s", CLAN_TRANSFER_STATE_FILE, exc)
