from __future__ import annotations

import asyncio
from copy import deepcopy
import re
from typing import Any

from elbow_helper.data.events import Event, finish_on_cancel, initialize_state, load_state, now_us, record, save_state

FALLBACK_THREADS_KEY = "_fallback_threads"
FALLBACK_INFO_MESSAGE_KEY = "_fallback_info_message_id"


class HibernationStateReader:
    """Read-only member state exposed to other features."""

    def __init__(self, state):
        self.state = state

    def get_member(self, member_id: int) -> dict[str, Any] | None:
        entry = self.state.snapshot().get(str(member_id))
        return entry if isinstance(entry, dict) else None


def hibernation_event(member_id, info, kind, *, actor_id=None, via="command", forced=False, ticket_id=None):
    return Event(
        "hibernation", f"{member_id}:{info['_case_started_at']}", kind,
        info["_case_started_at"] if kind == "hibernated" else now_us(),
        subject_discord_id=member_id, actor_discord_id=actor_id,
        actor_kind="import" if via == "import" else
        ("agent" if via == "agent" else "member") if actor_id is not None else "automatic",
        via=via, detail={"roles_taken": list(info["roles"])} if kind == "hibernated" else
        {"forced": forced, "ticket_channel_id": ticket_id},
        source_channel_id=ticket_id,
    )


class HibernationState:
    """Checkpoint each step with its events in SQLite."""

    def __init__(self, events):
        self.events = events
        self.state = {}
        self._lock = asyncio.Lock()
        self._members = {}

    def member_lock(self, member_id):
        return self._members.setdefault(member_id, asyncio.Lock())

    def snapshot(self):
        return deepcopy(self.state)

    async def startup(self):
        async with self._lock:
            async def complete():
                self.state = await self.events.call(self._load)
            await finish_on_cancel(complete())
        self.events.register_member_deletion("hibernation", self.refresh_after_deletion)

    def _load(self, connection):
        return initialize_state(connection, 'hibernation', dict)

    async def change(self, edit, events=()):
        async with self._lock:
            async def complete():
                def write(connection):
                    state = load_state(connection, "hibernation")
                    edit(state)
                    save_state(connection, "hibernation", state)
                    for event in events:
                        record(connection, event)
                    return state
                self.state = await self.events.call(write)
            await finish_on_cancel(complete())

    async def save_member(self, member_id, info, *, events=()):
        info = deepcopy(info)
        def edit(state):
            if info is None:
                state.pop(str(member_id), None)
            else:
                state[str(member_id)] = info
        await self.change(edit, events)

    async def save_fallback_info(self, message_id):
        await self.change(lambda state: set_fallback_info_message_id(state, message_id))

    async def save_fallback_thread(self, member_id, entry):
        entry = deepcopy(entry)
        def edit(state):
            if entry is None:
                remove_fallback_thread_entry(state, member_id)
            else:
                set_fallback_thread_entry(state, member_id, entry)
        await self.change(edit)

    async def refresh_after_deletion(self, discord_id):
        async with self._lock:
            self.state = await self.events.call(load_state, "hibernation")


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
