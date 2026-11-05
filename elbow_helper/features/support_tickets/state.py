"""The only writer of support and reactivation ticket registrations."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import logging

import discord

from elbow_helper.configuration.files import CREATED_TICKETS_FILE
from elbow_helper.data.events import Event, finish_on_cancel, load_state, now_us, record, save_state
from elbow_helper.infrastructure.persistence import read_json, write_json_atomic

LOGGER = logging.getLogger(__name__)
WORKFLOW = "support"


def ticket_event(channel_id, info, kind, *, at=None, actor_id=None, via="command"):
    return Event(
        WORKFLOW, str(channel_id), kind, now_us() if at is None else at,
        subject_discord_id=info.get("owner"), actor_discord_id=actor_id,
        actor_kind="import" if via == "import" else
        ("agent" if via == "agent" else "member") if actor_id is not None else "automatic",
        via=via, source_channel_id=channel_id,
        detail={"source": info.get("source", "reactivation")},
        sequence=info.get("_revision", 0),
    )


class TicketState:
    """SQLite owns registrations; JSON is a one-update revert mirror."""

    def __init__(self, events, path=None):
        self.events = events
        self.path = CREATED_TICKETS_FILE if path is None else path
        self.state = {}
        self._lock = asyncio.Lock()
        self._deleting = {}

    def registration(self, channel_id):
        return deepcopy(self.state.get(str(channel_id)))

    async def startup(self):
        async with self._lock:
            async def complete():
                self.state = await self.events.call(self._import)
                await self._mirror()
            await finish_on_cancel(complete())
        self.events.register_member_deletion(WORKFLOW, self.refresh_after_deletion)

    def _import(self, connection):
        state = load_state(connection, WORKFLOW)
        if state is not None:
            return state
        try:
            state = read_json(self.path)
        except FileNotFoundError:
            state = {}
        if not isinstance(state, dict):
            raise ValueError("Invalid ticket registration state")
        for channel, info in state.items():
            channel_id = int(channel)
            # created_by in old reactivation rows is the actor, not the owner.
            created = info.get("created_at")
            at = datetime.fromisoformat(created) if created else discord.utils.snowflake_time(channel_id)
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            record(connection, ticket_event(channel_id, info, "ticket_opened",
                                            at=int(at.timestamp() * 1_000_000), via="import"))
        save_state(connection, WORKFLOW, state)
        return state

    async def _change(self, edit):
        async with self._lock:
            async def complete():
                def write(connection):
                    state = load_state(connection, WORKFLOW)
                    result = edit(connection, state)
                    save_state(connection, WORKFLOW, state)
                    return state, result
                self.state, result = await self.events.call(write)
                await self._mirror()
                return result
            return await finish_on_cancel(complete())

    async def register(self, channel_id, info, *, actor_id, via="command"):
        info = deepcopy(info)
        def edit(connection, state):
            key = str(channel_id)
            if key in state:
                return False
            state[key] = info
            record(connection, ticket_event(channel_id, info, "ticket_opened", actor_id=actor_id, via=via))
            return True
        return await self._change(edit)

    async def transition(self, channel_id, status, *, actor_id=None, via="command"):
        if status not in {"closed", "open", "deleted"}:
            raise ValueError("Invalid ticket status")
        def edit(connection, state):
            info = state.get(str(channel_id))
            if info is None or info.get("_status", "open") == status:
                return False
            info["_revision"] = info.get("_revision", 0) + 1
            info["_status"] = status
            kind = {"closed": "ticket_closed", "open": "ticket_reopened", "deleted": "ticket_deleted"}[status]
            record(connection, ticket_event(channel_id, info, kind, actor_id=actor_id, via=via))
            if status == "deleted":
                del state[str(channel_id)]
            return True
        return await self._change(edit)

    async def refresh_after_deletion(self, discord_id):
        async with self._lock:
            self.state = await self.events.call(load_state, WORKFLOW)
            await self._mirror()

    async def delete_channel(self, channel, *, actor_id, via="button"):
        self._deleting[channel.id] = (actor_id, via)
        try:
            await channel.delete()
            await self.channel_deleted(channel.id)
        finally:
            self._deleting.pop(channel.id, None)

    async def channel_deleted(self, channel_id):
        actor_id, via = self._deleting.get(channel_id, (None, "schedule"))
        return await self.transition(channel_id, "deleted", actor_id=actor_id, via=via)

    async def _mirror(self):
        try:
            await asyncio.to_thread(write_json_atomic, self.path, self.state, indent=4)
        except (OSError, TypeError) as exc:
            LOGGER.error("Failed to save %s: %s", self.path, exc)
