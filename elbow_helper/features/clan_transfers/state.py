"""Disk-backed state helpers for clan transfers."""

from __future__ import annotations

import asyncio
from typing import Any

from elbow_helper.data.events import Event, finish_on_cancel, initialize_state, load_state, now_us, record, save_state
from .config import CLAN_TRANSFER_QUEUES
from .queries import _timestamp


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


def request_event(kind, clan_code, entry, *, at=None, actor_kind="automatic", actor_discord_id=None,
                  via="schedule", detail=None):
    started = int(_timestamp(entry["created_at"]).timestamp() * 1_000_000)
    return Event(
        "transfers", f"{entry['user_id']}:{clan_code}:{started}", kind,
        at if at is not None else now_us(), subject_discord_id=entry["user_id"], clan_code=clan_code,
        actor_kind=actor_kind, actor_discord_id=actor_discord_id, via=via,
        source_channel_id=CLAN_TRANSFER_QUEUES[clan_code]["thread_id"], detail=detail or {})


class TransferState:
    """Save transfer state and its events together in SQLite."""

    def __init__(self, events):
        self.events = events
        self.state = build_default_state()
        self._lock = asyncio.Lock()

    async def startup(self):
        async with self._lock:
            self.state = await self.events.call(self._load)
        self.events.register_member_deletion("transfers", self.refresh_after_deletion)

    def _load(self, connection):
        return initialize_state(connection, 'transfers', build_default_state)

    async def change(self, edit):
        async with self._lock:
            async def complete():
                self.state, result = await self.events.call(self._change, edit)
                return result
            return await finish_on_cancel(complete())

    @staticmethod
    def _change(connection, edit):
        state = load_state(connection, "transfers")
        result, events = edit(state)
        save_state(connection, "transfers", state)
        for event in events:
            record(connection, event)
        return state, result

    async def refresh_after_deletion(self, discord_id):
        async with self._lock:
            self.state = await self.events.call(load_state, "transfers")
