"""Member lifecycle state and history in the shared workflow database.

Join and leave times live in the family record's server membership, and last
messages come from the Discord collector. Lifecycle keeps only each join's
recruitment source and each departure's reason as events, plus the linger
reports, weekly source counts and ticket-log index it needs as state.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from typing import Any

from elbow_helper.configuration.channels import OVERSEEING_TERRACE
from elbow_helper.configuration.recruitment import PLATFORMS
from elbow_helper.data.events import Event, finish_on_cancel, initialize_state, load_state, now_us, record, save_state

WORKFLOW = "lifecycle"


def default_state() -> dict[str, Any]:
    return {
        "platform_counts": {},
        "last_weekly_report_iso": None,
        "last_applicant_scan_iso": None,
        "overdue_applicant_ids": [],
        "applicant_reports": {},
        "ticket_owner_links": {},
        "ticket_log_last_message_id": None,
        "ticket_log_index_ready": False,
    }


def microseconds(value: datetime) -> int:
    """The record's own conversion, so a stay and its lifecycle case share one join time."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1_000_000)


def joined_at_us(member) -> int:
    """Discord's join time for the member's current stay."""
    joined = getattr(member, "joined_at", None)
    return microseconds(joined) if joined is not None else now_us()


def lifecycle_event(kind, member_id, joined_us, detail, *, at=None, via="schedule", message_id=None):
    # Joins and departures come from Discord's own member events, never from message text.
    return Event(
        WORKFLOW, f"{int(member_id)}:{int(joined_us)}", kind, now_us() if at is None else at,
        subject_discord_id=int(member_id), actor_kind="import" if via == "import" else "automatic", via=via,
        detail=detail, source_channel_id=OVERSEEING_TERRACE if message_id else None,
        source_message_id=message_id or None,
    )


def open_joins(connection) -> dict[int, dict[str, Any]]:
    """Each member's newest join with no recorded departure from that stay."""
    joins = {}
    for row in connection.execute(
            "SELECT j.subject_discord_id,j.case_key,j.detail_json,j.source_message_id FROM workflow_events j "
            "WHERE j.workflow=? AND j.kind='joined' AND NOT EXISTS (SELECT 1 FROM workflow_events l "
            "WHERE l.workflow=j.workflow AND l.kind='left' AND l.case_key=j.case_key) "
            "ORDER BY j.occurred_at,j.id", (WORKFLOW,)):
        joins[row["subject_discord_id"]] = {
            "joined_at": int(row["case_key"].split(":")[1]),
            "platform": json.loads(row["detail_json"])["platform"],
            "message_id": row["source_message_id"],
        }
    return joins


class LifecycleState:
    """Save lifecycle state and its events together in SQLite."""

    def __init__(self, events):
        self.events = events
        self.state = default_state()
        self.joins: dict[int, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    # Readers return copies; the stored state is replaced, never changed in place.
    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self.state)

    def open_joins(self) -> dict[int, dict[str, Any]]:
        return deepcopy(self.joins)

    async def startup(self):
        async with self._lock:
            async def complete():
                self.state, self.joins = await self.events.call(self._load)
            await finish_on_cancel(complete())
        self.events.register_member_deletion(WORKFLOW, self.refresh_after_deletion)

    def _load(self, connection):
        state = initialize_state(connection, WORKFLOW, default_state)
        return state, open_joins(connection)

    async def change(self, edit):
        """Apply edit(connection, state) with the events it returns; None means nothing changed."""
        async with self._lock:
            async def complete():
                def write(connection):
                    state = load_state(connection, WORKFLOW)
                    events = edit(connection, state)
                    if events is None:
                        return None
                    save_state(connection, WORKFLOW, state)
                    for event in events:
                        record(connection, event)
                    return state, open_joins(connection)
                written = await self.events.call(write)
                if written is not None:
                    self.state, self.joins = written
                return written is not None
            return await finish_on_cancel(complete())

    async def update(self, values: dict[str, Any]) -> None:
        """Replace top-level state values, with no history."""
        values = deepcopy(values)

        def edit(connection, state):
            state.update(values)
            return ()
        await self.change(edit)

    async def joined(self, member_id: int, joined_us: int, platform: str, message_id: int | None) -> None:
        if platform not in PLATFORMS:
            raise ValueError("Unknown recruitment source")

        def edit(connection, state):
            counts = state["platform_counts"]
            counts[platform] = counts.get(platform, 0) + 1
            return (lifecycle_event("joined", member_id, joined_us, {"platform": platform},
                                    message_id=message_id),)
        await self.change(edit)

    async def left(self, member_id: int, joined_us: int, reason: str, message_id: int | None = None) -> None:
        def edit(connection, state):
            joined = open_joins(connection).get(member_id)
            if joined is not None:
                # As before, a tracked join leaving this week takes its source off the count.
                counts = state["platform_counts"]
                current = counts.get(joined["platform"], 0)
                if current > 1:
                    counts[joined["platform"]] = current - 1
                elif current == 1:
                    counts.pop(joined["platform"], None)
            case_joined = joined["joined_at"] if joined is not None else joined_us
            return (lifecycle_event("left", member_id, case_joined, {"reason": reason}, message_id=message_id),)
        await self.change(edit)

    async def refresh_after_deletion(self, discord_id):
        async with self._lock:
            def reload(connection):
                return load_state(connection, WORKFLOW), open_joins(connection)
            self.state, self.joins = await self.events.call(reload)
