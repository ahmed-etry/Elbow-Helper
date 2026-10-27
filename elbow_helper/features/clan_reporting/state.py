"""State persistence for clan data message tracking."""

from __future__ import annotations

import json
import logging
from typing import Any

from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic
from .config import CLANS, STATE_FILE


LOGGER = logging.getLogger(__name__)

ClanReportingState = dict[str, Any]


def load_state() -> ClanReportingState:
    """Load message IDs; a missing-elder ID of None means intentionally absent."""
    try:
        data = read_json(STATE_FILE)
    except FileNotFoundError:
        data = {}
    except (json.JSONDecodeError, OSError) as exc:
        LOGGER.warning("Failed to load state from %s: %s", STATE_FILE, exc)
        data = {}

    data.setdefault("missing_elder_messages", {})
    data.setdefault("war_summaries", {})
    data.setdefault("last_summary_month", None)
    # Legacy state only recorded a completed month for the whole clan set.
    data.setdefault("last_summary_month_by_clan", {
        clan_code: data["last_summary_month"]
        for clan_code in CLANS if data["last_summary_month"]
    })
    return data


def save_state(state: ClanReportingState) -> None:
    """Persist state so restarts keep tracked message IDs."""
    try:
        write_json_atomic(STATE_FILE, state, indent=2)
    except (OSError, TypeError) as exc:
        LOGGER.error("Failed to save state to %s: %s", STATE_FILE, exc)
