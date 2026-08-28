"""Disk-backed state helpers for recruitment workflows."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict

from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic
from .config import APPLICANT_AI_FILE
from .config import TICKET_ACTIVITY_FILE
from .config import TRIAL_DATA_FILE
from .config import TRIAL_REMINDERS_FILE

LOGGER = logging.getLogger(__name__)


class RecruitmentStateStore:
    """Own every persisted recruitment workflow document."""

    def load_trial_data(self) -> Dict[str, Any]:
        try:
            return read_json(TRIAL_DATA_FILE)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save_trial_data(self, data: Dict[str, Any]) -> None:
        write_json_atomic(TRIAL_DATA_FILE, data, indent=2)

    def load_trial_reminders(self) -> Dict[str, Any]:
        try:
            return read_json(TRIAL_REMINDERS_FILE)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save_trial_reminders(self, data: Dict[str, Any]) -> None:
        write_json_atomic(TRIAL_REMINDERS_FILE, data, indent=2)

    def load_ticket_activity(self) -> Dict[int, Dict[str, Any]]:
        try:
            data = read_json(TICKET_ACTIVITY_FILE)
            parsed: Dict[int, Dict[str, Any]] = {}
            for key, value in data.items():
                channel_id = int(key)
                if isinstance(value, dict):
                    last_at = value.get("last_reminder_at")
                    if not last_at:
                        continue
                    message_id = value.get("last_applicant_message_id")
                    if message_id is not None:
                        try:
                            message_id = int(message_id)
                        except (TypeError, ValueError):
                            message_id = None
                    parsed[channel_id] = {
                        "last_reminder_at": datetime.fromisoformat(last_at),
                        "message_id": value.get("message_id"),
                        "message_channel_id": value.get("message_channel_id"),
                        "last_applicant_message_id": message_id,
                    }
                else:
                    parsed[channel_id] = {
                        "last_reminder_at": datetime.fromisoformat(value),
                        "message_id": None,
                        "message_channel_id": None,
                        "last_applicant_message_id": None,
                    }
            return parsed
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save_ticket_activity(
        self,
        data: Dict[int, Dict[str, Any]],
    ) -> None:
        payload: Dict[str, Any] = {}
        for channel_id, info in data.items():
            last_at = info.get("last_reminder_at")
            if not last_at:
                continue
            payload[str(channel_id)] = {
                "last_reminder_at": last_at.isoformat(),
                "message_id": info.get("message_id"),
                "message_channel_id": info.get("message_channel_id"),
                "last_applicant_message_id": info.get(
                    "last_applicant_message_id"
                ),
            }
        write_json_atomic(TICKET_ACTIVITY_FILE, payload, indent=4)

    def load_applicant_ai_messages(self) -> Dict[str, Dict[str, Any]]:
        try:
            if os.path.exists(APPLICANT_AI_FILE):
                data = read_json(APPLICANT_AI_FILE)
                if isinstance(data, dict):
                    return data
            return {}
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            LOGGER.exception(
                "Failed loading applicant AI messages: %s",
                error,
            )
            return {}

    def save_applicant_ai_messages(
        self,
        data: Dict[str, Dict[str, Any]],
    ) -> None:
        try:
            write_json_atomic(APPLICANT_AI_FILE, data, indent=2)
        except (OSError, TypeError, ValueError) as error:
            LOGGER.exception(
                "Failed saving applicant AI messages: %s",
                error,
            )
