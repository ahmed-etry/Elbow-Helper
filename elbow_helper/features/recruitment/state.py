"""Recruitment workflow state and history in the shared workflow database."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import logging
from typing import Any, Dict

from elbow_helper.data.events import Event, finish_on_cancel, initialize_state, load_state, now_us, record, save_state

LOGGER = logging.getLogger(__name__)
WORKFLOW = "recruitment"


def default_state():
    return {part: {} for part in ("trials", "reminders", "ticket_activity", "ai_messages")}


def parse_ticket_activity(data: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """Stored shape to the in-memory shape the reminder loops use."""
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


def serialize_ticket_activity(data: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for channel_id, info in data.items():
        last_at = info.get("last_reminder_at")
        if not last_at:
            continue
        payload[str(channel_id)] = {
            "last_reminder_at": last_at.isoformat(),
            "message_id": info.get("message_id"),
            "message_channel_id": info.get("message_channel_id"),
            "last_applicant_message_id": info.get("last_applicant_message_id"),
        }
    return payload


def trial_started_at(info: Dict[str, Any]) -> int:
    """A trial's case identity: its start in microseconds."""
    start = datetime.fromisoformat(info["start"])
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return int(start.timestamp() * 1_000_000)


def _actor(actor_id, via):
    if via == "import":
        return "import"
    if actor_id is None:
        return "automatic"
    return "agent" if via == "agent" else "member"


def recruitment_event(kind, ticket_id, case_started_at, *, applicant_id=None, actor_id=None,
                      via="schedule", detail=None, at=None):
    try:
        applicant_id = int(applicant_id) or None
    except (TypeError, ValueError):
        applicant_id = None
    return Event(
        WORKFLOW, f"{int(ticket_id)}:{int(case_started_at)}", kind,
        now_us() if at is None else at, subject_discord_id=applicant_id,
        actor_discord_id=actor_id, actor_kind=_actor(actor_id, via), via=via,
        source_channel_id=int(ticket_id), detail=detail or {},
    )


def trial_event(kind, ticket_id, info, *, actor_id=None, via="schedule", detail=None, at=None):
    return recruitment_event(kind, ticket_id, trial_started_at(info), applicant_id=info.get("applicant_id"),
                             actor_id=actor_id, via=via, detail=detail, at=at)


def reminder_case(reminder: Dict[str, Any]) -> int:
    """A trial-end reminder belongs to its trial's case, or to its creation for imported reminders."""
    return reminder.get("_case_started_at") or int(reminder.get("created_at") or 0) * 1_000_000 or now_us()


def trial_events(kind, ticket_id, info, **values):
    """A trial's event, or none for an old entry without a readable start."""
    try:
        return (trial_event(kind, ticket_id, info, **values),)
    except (AttributeError, KeyError, TypeError, ValueError):
        LOGGER.warning("Trial %s changed without a %s event", ticket_id, kind)
        return ()


def decision_event(kind, ticket_id, applicant_id, *, actor_id, via, detail=None):
    """Accept, decline, finalize and checkup are each a case of their own."""
    at = now_us()
    return recruitment_event(kind, ticket_id, at, applicant_id=applicant_id, actor_id=actor_id,
                             via=via, detail=detail, at=at)


class RecruitmentState:
    """Save recruitment state and its events together in SQLite."""

    def __init__(self, events):
        self.events = events
        self.state = default_state()
        self._lock = asyncio.Lock()

    # Readers return copies; the stored state is replaced, never changed in place.
    def trials(self) -> Dict[str, Any]:
        return deepcopy(self.state["trials"])

    def reminders(self) -> Dict[str, Any]:
        return deepcopy(self.state["reminders"])

    def ticket_activity_records(self) -> Dict[str, Any]:
        return deepcopy(self.state["ticket_activity"])

    def ticket_activity(self) -> Dict[int, Dict[str, Any]]:
        return parse_ticket_activity(self.state["ticket_activity"])

    def ai_messages(self) -> Dict[str, Dict[str, Any]]:
        return deepcopy(self.state["ai_messages"])

    async def startup(self):
        async with self._lock:
            async def complete():
                self.state = await self.events.call(self._load)
            await finish_on_cancel(complete())
        self.events.register_member_deletion(WORKFLOW, self.refresh_after_deletion)

    def _load(self, connection):
        return initialize_state(connection, 'recruitment', default_state)

    async def change(self, edit):
        """Apply edit(state) with the events it returns; None means nothing changed."""
        async with self._lock:
            async def complete():
                def write(connection):
                    state = load_state(connection, WORKFLOW)
                    events = edit(state)
                    if events is None:
                        return state, False
                    save_state(connection, WORKFLOW, state)
                    for event in events:
                        record(connection, event)
                    return state, True
                self.state, changed = await self.events.call(write)
                return changed
            return await finish_on_cancel(complete())

    async def record_event(self, event):
        """History of a finished Discord action that changes no state."""
        async with self._lock:
            return await finish_on_cancel(self.events.call(record, event))

    async def save_ticket_activity(self, data: Dict[int, Dict[str, Any]]) -> None:
        payload = serialize_ticket_activity(data)
        def edit(state):
            state["ticket_activity"] = payload
            return ()
        await self.change(edit)

    async def save_ai_messages(self, data: Dict[str, Dict[str, Any]]) -> None:
        payload = deepcopy(data)
        def edit(state):
            state["ai_messages"] = payload
            return ()
        await self.change(edit)

    async def refresh_after_deletion(self, discord_id):
        async with self._lock:
            self.state = await self.events.call(load_state, WORKFLOW)
