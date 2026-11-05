"""Recruitment workflow state and history in the shared workflow database."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import logging
from typing import Any, Dict

from elbow_helper.data.events import Event, finish_on_cancel, load_state, now_us, record, save_state
from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic
from .config import APPLICANT_AI_FILE
from .config import TICKET_ACTIVITY_FILE
from .config import TRIAL_DATA_FILE
from .config import TRIAL_DAYS_DEFAULT
from .config import TRIAL_REMINDERS_FILE

LOGGER = logging.getLogger(__name__)
WORKFLOW = "recruitment"
# Each part is mirrored to its old file for one update, so a revert keeps open work.
LEGACY_FILES = {
    "trials": TRIAL_DATA_FILE,
    "reminders": TRIAL_REMINDERS_FILE,
    "ticket_activity": TICKET_ACTIVITY_FILE,
    "ai_messages": APPLICANT_AI_FILE,
}
INDENTS = {"ticket_activity": 4}


def parse_ticket_activity(data: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """Old file and stored shape to the in-memory shape the reminder loops use."""
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


def _legacy(path) -> Dict[str, Any]:
    try:
        data = read_json(path)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Invalid recruitment state")
    return data


class RecruitmentState:
    """SQLite owns recruitment state with its events; JSON files are a one-update revert mirror."""

    def __init__(self, events, paths=None):
        self.events = events
        self.paths = {**LEGACY_FILES, **(paths or {})}
        self.state = {part: {} for part in LEGACY_FILES}
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
                self.state = await self.events.call(self._import)
                await self._mirror(None)
            await finish_on_cancel(complete())
        self.events.register_member_deletion(WORKFLOW, self.refresh_after_deletion)

    def _import(self, connection):
        state = load_state(connection, WORKFLOW)
        if state is not None:
            return state
        trials = _legacy(self.paths["trials"])
        reminders = _legacy(self.paths["reminders"])
        activity = serialize_ticket_activity(parse_ticket_activity(_legacy(self.paths["ticket_activity"])))
        try:
            ai_messages = _legacy(self.paths["ai_messages"])
        except ValueError as error:
            # As before the move, unreadable AI message IDs only skip their cleanup.
            LOGGER.error("Failed loading applicant AI messages: %s", error)
            ai_messages = {}
        started = 0
        for ticket, info in trials.items():
            try:
                started += record(connection, trial_event(
                    "trial_started", ticket, info, via="import", at=trial_started_at(info),
                    detail={"days": int(info.get("days", TRIAL_DAYS_DEFAULT))}))
            except (AttributeError, KeyError, TypeError, ValueError):
                # The trial stays open in state; only its start history is unknown.
                LOGGER.warning("Imported trial %s without a start event", ticket)
        for info in reminders.values():
            if isinstance(info, dict):
                # The trial start is gone once a reminder exists; its creation names the case.
                info["_case_started_at"] = reminder_case(info)
        state = {"trials": trials, "reminders": reminders, "ticket_activity": activity,
                 "ai_messages": ai_messages}
        save_state(connection, WORKFLOW, state)
        LOGGER.info("Imported %s open trials (%s start events), %s trial reminders, %s ticket reminders "
                    "and %s AI messages", len(trials), started, len(reminders), len(activity), len(ai_messages))
        return state

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
                previous = self.state
                self.state, changed = await self.events.call(write)
                if changed:
                    await self._mirror(previous)
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
            previous = self.state
            self.state = await self.events.call(load_state, WORKFLOW)
            await self._mirror(previous)

    async def _mirror(self, previous):
        for part, path in self.paths.items():
            if previous is not None and previous.get(part) == self.state[part]:
                continue
            try:
                await asyncio.to_thread(write_json_atomic, path, self.state[part],
                                        indent=INDENTS.get(part, 2))
            except (OSError, TypeError) as exc:
                # SQLite stays authoritative; startup and the next change retry the mirror.
                LOGGER.error("Failed saving %s: %s", path, exc)
