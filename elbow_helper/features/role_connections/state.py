"""JSON persistence for role connection rules."""

from __future__ import annotations

import json
import logging
from typing import Any

from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic
from .config import STATE_FILE

LOGGER = logging.getLogger(__name__)


def load_state() -> dict[str, Any]:
    try:
        data = read_json(STATE_FILE)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    except OSError as exc:
        LOGGER.warning("Failed loading %s: %s", STATE_FILE, exc)
        data = {}

    data.setdefault("connections", [])
    return data


def save_state(state: dict[str, Any]) -> None:
    try:
        write_json_atomic(STATE_FILE, state, indent=2)
    except (OSError, TypeError) as exc:
        LOGGER.exception("Failed saving %s: %s", STATE_FILE, exc)
        raise

