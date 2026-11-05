"""Disk-backed state helpers for clan transfers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic
from elbow_helper.data.events import Event, finish_on_cancel, load_state, now_us, record, save_state
from .config import CLAN_TRANSFER_QUEUES
from .config import CLAN_TRANSFER_STATE_FILE
from .config import REQUEST_TTL_HOURS
from .queries import ClanTransferQueries, _timestamp

LOGGER = logging.getLogger(__name__)


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


def _legacy_state(path) -> dict[str, Any]:
    try:
        data = read_json(path)
    except FileNotFoundError:
        return build_default_state()

    if not isinstance(data, dict):
        raise ValueError("Invalid transfer queue state")

    data.setdefault("global_board_message_id", None)
    data.setdefault("clans", {})
    for code in CLAN_TRANSFER_QUEUES:
        existing = data["clans"].get(code, {})
        merged = {**default_clan_state(code), **existing}
        merged.setdefault("pending", [])
        data["clans"][code] = merged
    ClanTransferQueries(lambda: data).pending_snapshot(clan_codes=tuple(CLAN_TRANSFER_QUEUES))
    return data


def request_event(kind, clan_code, entry, *, at=None, actor_kind="automatic", actor_discord_id=None,
                  via="schedule", detail=None):
    started = int(_timestamp(entry["created_at"]).timestamp() * 1_000_000)
    return Event(
        "transfers", f"{entry['user_id']}:{clan_code}:{started}", kind,
        at if at is not None else now_us(), subject_discord_id=entry["user_id"], clan_code=clan_code,
        actor_kind=actor_kind, actor_discord_id=actor_discord_id, via=via,
        source_channel_id=CLAN_TRANSFER_QUEUES[clan_code]["thread_id"], detail=detail or {})


class TransferState:
    """The SQLite writer owns state changes; JSON is a one-update revert mirror."""

    def __init__(self, events, path=None, *, clock=None):
        self.events = events
        self.path = path if path is not None else CLAN_TRANSFER_STATE_FILE
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.state = build_default_state()
        self._lock = asyncio.Lock()

    async def startup(self):
        async with self._lock:
            self.state = await self.events.call(self._import)
            await self._mirror()
        self.events.register_member_deletion("transfers", self.refresh_after_deletion)

    def _import(self, connection):
        state = load_state(connection, "transfers")
        if state is None:
            state = _legacy_state(self.path)
            now = self.clock()
            for code, queue in state["clans"].items():
                # Expired legacy requests are no longer open at the move.
                queue["pending"] = [entry for entry in queue["pending"]
                                    if _timestamp(entry["created_at"]) + timedelta(hours=REQUEST_TTL_HOURS) > now]
                for entry in queue["pending"]:
                    event = request_event("transfer_requested", code, entry,
                                          at=int(_timestamp(entry["created_at"]).timestamp() * 1_000_000),
                                          actor_kind="import", via="import")
                    record(connection, event)
            save_state(connection, "transfers", state)
        return state

    async def change(self, edit):
        async with self._lock:
            async def complete():
                self.state, result = await self.events.call(self._change, edit)
                await self._mirror()
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
            await self._mirror()

    async def _mirror(self):
        try:
            await asyncio.to_thread(write_json_atomic, self.path, self.state, indent=2)
        except (OSError, TypeError) as exc:
            # SQLite remains authoritative. Startup and the next change retry the mirror.
            LOGGER.error("Failed to save %s: %s", self.path, exc)
