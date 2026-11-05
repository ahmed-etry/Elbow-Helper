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
import logging
from typing import Any

from elbow_helper.configuration.channels import OVERSEEING_TERRACE
from elbow_helper.configuration.recruitment import PLATFORMS
from elbow_helper.data.events import Event, finish_on_cancel, load_state, now_us, record, save_state
from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic
from .config import STATE_FILE

LOGGER = logging.getLogger(__name__)
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


STATE_TYPES = {
    "platform_counts": dict, "overdue_applicant_ids": list, "applicant_reports": dict,
    "ticket_owner_links": dict, "ticket_log_index_ready": bool,
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


def _legacy(path) -> dict[str, Any]:
    try:
        data = read_json(path)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Invalid member lifecycle state")
    return data


def _last_seen(old: dict[str, Any]) -> dict[str, Any]:
    # Kept in the file unchanged until the family record has copied it; never lifecycle state.
    value = old.get("last_seen", {})
    return value if isinstance(value, dict) else {}


def _iso(at: int) -> str:
    return datetime.fromtimestamp(at / 1_000_000, timezone.utc).isoformat()


class LifecycleState:
    """SQLite owns lifecycle state with its events; the old JSON is a one-update revert mirror."""

    def __init__(self, events, path=None):
        self.events = events
        self.path = STATE_FILE if path is None else path
        self.state = default_state()
        self.joins: dict[int, dict[str, Any]] = {}
        self._last_seen: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    # Readers return copies; the stored state is replaced, never changed in place.
    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self.state)

    def open_joins(self) -> dict[int, dict[str, Any]]:
        return deepcopy(self.joins)

    async def startup(self):
        async with self._lock:
            async def complete():
                self.state, self.joins = await self.events.call(self._import)
                await self._mirror()
            await finish_on_cancel(complete())
        self.events.register_member_deletion(WORKFLOW, self.refresh_after_deletion)

    def _import(self, connection):
        state = load_state(connection, WORKFLOW)
        if state is not None:
            try:
                self._last_seen = _last_seen(_legacy(self.path))
            except (OSError, ValueError):
                self._last_seen = {}
            return state, open_joins(connection)
        old = _legacy(self.path)
        self._last_seen = _last_seen(old)
        state = default_state()
        for key in state:
            if old.get(key) is not None:
                state[key] = old[key]
        for key, kind in STATE_TYPES.items():
            if not isinstance(state[key], kind):
                raise ValueError("Invalid member lifecycle state")
        # A closed report's button is already disabled; only open ones are work.
        reports = state["applicant_reports"]
        state["applicant_reports"] = {key: value for key, value in reports.items()
                                      if isinstance(value, dict) and value.get("active") is True}
        members = old.get("members", {})
        if not isinstance(members, dict):
            raise ValueError("Invalid member lifecycle state")
        imported = closed = skipped = 0
        for key, info in members.items():
            if isinstance(info, dict) and info.get("left") is True:
                closed += 1
                continue
            try:
                member_id = int(key)
                joined = microseconds(datetime.fromisoformat(info["joined_at_iso"]))
                message_id = info.get("overseer_message_id")
                if type(message_id) is not int or message_id <= 0:
                    message_id = None
                imported += record(connection, lifecycle_event(
                    "joined", member_id, joined, {"platform": info["platform"]},
                    at=joined, via="import", message_id=message_id))
            except (KeyError, TypeError, ValueError):
                skipped += 1
        save_state(connection, WORKFLOW, state)
        LOGGER.info("Imported %s open member joins (%s closed and %s unreadable left out), %s open applicant "
                    "reports (%s closed left out) and %s ticket-log owners",
                    imported, closed, skipped, len(state["applicant_reports"]),
                    len(reports) - len(state["applicant_reports"]), len(state["ticket_owner_links"]))
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
                    await self._mirror()
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
            self._last_seen.pop(str(discord_id), None)
            await self._mirror()

    def legacy_payload(self) -> dict[str, Any]:
        """The old file's shape, so a revert keeps open joins, reports and counts."""
        members = {str(member_id): {"overseer_message_id": join["message_id"], "platform": join["platform"],
                                    "joined_at_iso": _iso(join["joined_at"]), "left": False}
                   for member_id, join in sorted(self.joins.items())}
        return {**self.state, "members": members, "last_seen": self._last_seen}

    async def _mirror(self):
        try:
            await asyncio.to_thread(write_json_atomic, self.path, self.legacy_payload(), indent=2)
        except (OSError, TypeError) as exc:
            # SQLite stays authoritative; startup and the next change retry the mirror.
            LOGGER.error("Failed saving %s: %s", self.path, exc)
