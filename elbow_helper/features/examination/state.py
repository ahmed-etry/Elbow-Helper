"""Persistent examination state storage."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic
from .config import STATE_FILE


LOGGER = logging.getLogger(__name__)


class ExaminationStateStore:
    """Own the complete persisted examination workflow state."""

    def __init__(self) -> None:
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        if not os.path.exists(STATE_FILE):
            data: dict[str, Any] = {}
        else:
            try:
                data = read_json(STATE_FILE)
            except (json.JSONDecodeError, OSError, TypeError):
                LOGGER.exception("State load failed: path=%s", STATE_FILE)
                data = {}
        data.setdefault("examiner_roster", {})
        data.setdefault("cases", {})
        data.setdefault("panel_message_id", None)
        data.setdefault("deprecated_routing_messages", [])
        data.setdefault("timezone_preferences", {})
        return data

    def save(self) -> None:
        try:
            write_json_atomic(STATE_FILE, self.state, indent=2)
        except (OSError, TypeError):
            LOGGER.exception("State save failed: path=%s", STATE_FILE)

    def examiner_roster(self) -> dict[str, Any]:
        return self.state.setdefault("examiner_roster", {})

    def cases(self) -> dict[str, Any]:
        return self.state.setdefault("cases", {})

    def case(self, channel_id: int) -> dict[str, Any] | None:
        return self.cases().get(str(channel_id))

    def deprecated_routing_messages(self) -> list[dict[str, Any]]:
        queue = self.state.setdefault("deprecated_routing_messages", [])
        if not isinstance(queue, list):
            queue = []
            self.state["deprecated_routing_messages"] = queue
        return queue
