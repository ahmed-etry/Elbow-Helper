from __future__ import annotations

import json
import logging
from typing import Any

from elbow_helper.configuration.files import CREATED_TICKETS_FILE
from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic

LOGGER = logging.getLogger(__name__)


def load_tickets() -> dict[str, Any]:
    if not CREATED_TICKETS_FILE.exists():
        return {}
    try:
        return read_json(CREATED_TICKETS_FILE)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Failed to load %s: %s", CREATED_TICKETS_FILE, exc)
        return {}


def save_tickets(data: dict[str, Any]) -> None:
    try:
        write_json_atomic(CREATED_TICKETS_FILE, data, indent=4)
    except (OSError, TypeError) as exc:
        LOGGER.error("Failed to save %s: %s", CREATED_TICKETS_FILE, exc)
        raise
