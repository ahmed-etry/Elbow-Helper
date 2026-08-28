from __future__ import annotations

import json
import logging
from typing import Any

from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic
from .config import STATE_FILE

LOGGER = logging.getLogger(__name__)


def default_state() -> dict[str, Any]:
    return {
        "members": {},
        "last_seen": {},
        "platform_counts": {},
        "last_weekly_report_iso": None,
        "last_applicant_scan_iso": None,
        "applicant_reports": {},
        "ticket_owner_links": {},
        "ticket_log_last_message_id": None,
        "ticket_log_index_ready": False,
    }


def load_state() -> dict[str, Any]:
    try:
        return read_json(STATE_FILE)
    except FileNotFoundError:
        return default_state()
    except json.JSONDecodeError:
        return default_state()
    except OSError as exc:
        LOGGER.warning("Failed loading %s: %s", STATE_FILE, exc)
        return default_state()


def save_state(state: dict[str, Any]) -> None:
    try:
        write_json_atomic(STATE_FILE, state, indent=2)
    except (OSError, TypeError) as exc:
        LOGGER.error("Failed saving %s: %s", STATE_FILE, exc)
