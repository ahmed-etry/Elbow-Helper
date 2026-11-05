"""Examination cases, roster and panel in the shared workflow database."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import logging
import sqlite3
from typing import Any

import discord

from elbow_helper.configuration.channels import EXAMINATION_ROOM
from elbow_helper.configuration.clans import CLANS
from elbow_helper.data.events import (
    Event, finish_on_cancel, load_cases, load_state, now_us, record, save_case, save_state, scrub_member,
)
from elbow_helper.data.events.records import validate
from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic
from .config import STATE_FILE


LOGGER = logging.getLogger(__name__)
WORKFLOW = "examination"
# A failed save keeps its cases unsaved in memory and tries again after this.
RETRY_SECONDS = 60
META_DEFAULTS = {
    "examiner_roster": dict, "panel_message_id": lambda: None,
    "deprecated_routing_messages": list, "timezone_preferences": dict,
}
# Kinds that happen once per case; the others repeat and are keyed by their time.
ONCE = frozenset({"started", "closed"})


def _at(value: Any) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1_000_000)


def _member(value: Any) -> int | None:
    return value if type(value) is int and 0 < value < 2**63 else None


def _channel(key: Any) -> int | None:
    text = str(key)
    return int(text) if text.isascii() and text.isdecimal() and 0 < int(text) < 2**63 else None


def examination_event(kind, channel_id, case, *, at=None, actor_id=None, via="schedule",
                      detail=None, message_id=None, clan=None):
    at = now_us() if at is None else at
    actor_id = _member(actor_id)
    if via == "import":
        actor_kind = "import"
    elif actor_id is None:
        actor_kind = "automatic"
    else:
        actor_kind = "agent" if via == "agent" else "member"
    return Event(
        WORKFLOW, str(channel_id), kind, at, actor_kind=actor_kind, via=via,
        subject_discord_id=_member(case.get("opener_id")), actor_discord_id=actor_id,
        clan_code=clan if clan in CLANS else None, detail=detail or {},
        source_channel_id=EXAMINATION_ROOM if message_id else channel_id, source_message_id=message_id,
        sequence=0 if kind in ONCE else at,
    )


def stage_events(channel_id: int, before: dict | None, after: dict | None) -> list[Event]:
    """The stages one saved change of a case reached; what changed in between is state only."""
    if after is None:
        return [] if before is None else [examination_event("closed", channel_id, before)]
    previous = before or {}
    target = after.get("to_clan")
    events = []
    if before is None:
        events.append(examination_event("started", channel_id, after, at=_at(after.get("created_at")),
                                        detail={"case_type": after.get("type")}))
    changed_at = after.get("route_change_requested_at")
    route_changed = bool(changed_at) and changed_at != previous.get("route_change_requested_at")
    if route_changed:
        events.append(examination_event(
            "route_changed", channel_id, after, at=_at(changed_at),
            actor_id=after.get("route_change_requested_by"), via=after.get("route_change_via") or "button",
            detail={"reopened": after.get("intake_state") != "complete"}))
    if (after.get("type") == "clan_promo" and after.get("intake_state") == "complete"
            and previous.get("intake_state") != "complete" and not route_changed):
        events.append(examination_event(
            "answered", channel_id, after, at=_at(after.get("intake_completed_at")),
            actor_id=after.get("opener_id"), via="button", clan=target,
            detail={"exam_required": after.get("exam_required") is True}))
    structured = after.get("availability_structured")
    if structured and structured != previous.get("availability_structured"):
        events.append(examination_event(
            "availability_given", channel_id, after, at=_at(after.get("availability_set_at")),
            actor_id=after.get("availability_set_by"), via="button"))
    routing_id = after.get("routing_message_id")
    if routing_id and routing_id != previous.get("routing_message_id"):
        events.append(examination_event(
            "sent_for_review", channel_id, after, at=_at(after.get("routing_created_at")),
            message_id=_member(routing_id), clan=target,
            detail={"exam_required": after.get("exam_required", True) is not False,
                    "missing_answers": after.get("stage") == "missing"}))
    stage = after.get("stage")
    if stage in ("reminder", "fallback") and stage != previous.get("stage"):
        events.append(examination_event("followed_up", channel_id, after, at=_at(after.get("last_ping_at")),
                                        detail={"stage": stage}))
    if after.get("responded") is True and previous.get("responded") is not True:
        events.append(examination_event("reviewed", channel_id, after, at=_at(after.get("responded_at")),
                                        actor_id=after.get("responded_by"), via="message"))
    valid = []
    for event in events:
        try:
            validate(event)
        except ValueError:
            # The case still saves; only this stage's history is unknown.
            LOGGER.warning("Examination case %s saved without its %s event", channel_id, event.kind)
            continue
        valid.append(event)
    return valid


def _legacy(path) -> dict[str, Any]:
    try:
        data = read_json(path)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Invalid examination state")
    return data


def _with_defaults(meta: dict[str, Any]) -> dict[str, Any]:
    for key, default in META_DEFAULTS.items():
        meta.setdefault(key, default())
    return meta


def _history(connection, cases) -> dict[int, list[tuple[str, int]]]:
    history: dict[int, list[tuple[str, int]]] = {}
    for case_key, kind, at in connection.execute(
            "SELECT case_key,kind,occurred_at FROM workflow_events WHERE workflow=? AND kind<>'voided' "
            "ORDER BY occurred_at,id", (WORKFLOW,)):
        if case_key in cases:
            history.setdefault(int(case_key), []).append((kind, at))
    return history


class ExaminationStateStore:
    """SQLite owns examination state, one row per case; the JSON file is a one-update revert mirror.

    The cog changes ``state`` in place and calls ``save()``; a background flush then writes only the
    cases that changed, with the stages they reached, in one transaction off the event loop."""

    def __init__(self, events, path=None) -> None:
        self.events = events
        self.path = STATE_FILE if path is None else path
        self.state: dict[str, Any] = _with_defaults({"cases": {}})
        self._saved_cases: dict[str, dict[str, Any]] = {}
        self._saved_meta: dict[str, Any] = _with_defaults({})
        self._history: dict[int, list[tuple[str, int]]] = {}
        self._lock = asyncio.Lock()
        self._requested = False
        self._flush_task: asyncio.Task | None = None
        self._closed = False

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

    def saved_cases(self) -> dict[str, dict[str, Any]]:
        """Cases as committed; entries are replaced on save, never changed in place."""
        return dict(self._saved_cases)

    def stage_history(self) -> dict[int, tuple[tuple[str, int], ...]]:
        """Recorded stages of each open case: kind and time in microseconds, oldest first."""
        return {channel_id: tuple(rows) for channel_id, rows in self._history.items()}

    async def startup(self) -> None:
        async with self._lock:
            async def complete():
                meta, cases, history = await self.events.call(self._import)
                self._publish(meta, cases, history)
                await self._mirror()
            await finish_on_cancel(complete())
        self.events.register_member_deletion(WORKFLOW, self.refresh_after_deletion)

    def close(self) -> None:
        self._closed = True
        self.events.unregister_member_deletion(WORKFLOW)

    def _import(self, connection):
        meta = load_state(connection, WORKFLOW)
        if meta is None:
            legacy = _legacy(self.path)
            cases = legacy.pop("cases", {})
            if not isinstance(cases, dict):
                raise ValueError("Invalid examination cases")
            meta = _with_defaults(legacy)
            imported = started = skipped = 0
            for key, case in cases.items():
                channel_id = _channel(key)
                if channel_id is None or not isinstance(case, dict):
                    skipped += 1
                    continue
                save_case(connection, channel_id, case)
                imported += 1
                event = examination_event(
                    "started", channel_id, case, via="import", detail={"case_type": case.get("type")},
                    at=int(discord.utils.snowflake_time(channel_id).timestamp() * 1_000_000))
                try:
                    started += record(connection, event)
                except ValueError:
                    LOGGER.warning("Imported examination case %s without a start event", channel_id)
            save_state(connection, WORKFLOW, meta)
            LOGGER.info("Imported %s open examination cases (%s start events); skipped %s unreadable",
                        imported, started, skipped)
        cases = load_cases(connection)
        return _with_defaults(meta), cases, _history(connection, cases)

    def _publish(self, meta, cases, history) -> None:
        # The cog holds this dict, so it is refilled in place.
        self._saved_meta, self._saved_cases, self._history = meta, cases, history
        self.state.clear()
        self.state.update(deepcopy(meta))
        self.state["cases"] = deepcopy(cases)

    def save(self) -> None:
        """Ask for the changed cases to be saved; callers stay synchronous."""
        self._requested = True
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._flush_task = loop.create_task(self._flush_requests(), name="examination-state-save")

    async def _flush_requests(self) -> None:
        while self._requested:
            self._requested = False
            if not await self.flush():
                if self._closed:
                    return
                await asyncio.sleep(RETRY_SECONDS)
                if self._closed:
                    return
                self._requested = True

    async def flush(self) -> bool:
        """Write every case changed since the last save; False keeps them for a retry."""
        async with self._lock:
            return await finish_on_cancel(self._write_changes())

    async def _write_changes(self) -> bool:
        live = self.state.get("cases")
        live = live if isinstance(live, dict) else {}
        changed: dict[str, dict[str, Any] | None] = {}
        for key, case in live.items():
            if _channel(key) is None or not isinstance(case, dict):
                continue
            if self._saved_cases.get(key) != case:
                changed[key] = deepcopy(case)
        for key in self._saved_cases.keys() - live.keys():
            changed[key] = None
        meta = {key: deepcopy(value) for key, value in self.state.items() if key != "cases"}
        meta_changed = meta != self._saved_meta
        if not changed and not meta_changed:
            return True
        events = [event for key, case in changed.items()
                  for event in stage_events(int(key), self._saved_cases.get(key), case)]

        def write(connection):
            for key, case in changed.items():
                save_case(connection, int(key), case)
            if meta_changed:
                save_state(connection, WORKFLOW, meta)
            return [event for event in events if record(connection, event)]

        try:
            recorded = await self.events.call(write)
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            LOGGER.error("Failed saving %s examination cases: %s", len(changed), exc)
            return False
        for key, case in changed.items():
            if case is None:
                self._saved_cases.pop(key, None)
                self._history.pop(int(key), None)
            else:
                self._saved_cases[key] = case
        if meta_changed:
            self._saved_meta = meta
        for event in recorded:
            channel_id = int(event.case_key)
            if str(channel_id) in self._saved_cases:
                self._history.setdefault(channel_id, []).append((event.kind, event.occurred_at))
                self._history[channel_id].sort(key=lambda row: row[1])
        await self._mirror()
        return True

    async def refresh_after_deletion(self, discord_id: int) -> None:
        async with self._lock:
            async def complete():
                def reload(connection):
                    cases = load_cases(connection)
                    return load_state(connection, WORKFLOW), cases, _history(connection, cases)
                meta, cases, history = await self.events.call(reload)
                # Unsaved changes stay unsaved, with the member removed the same way.
                live = self.state.setdefault("cases", {})
                scrub_member(live, self.state, discord_id)
                self._saved_meta, self._saved_cases, self._history = _with_defaults(meta or {}), cases, history
                await self._mirror()
            await finish_on_cancel(complete())

    async def _mirror(self) -> None:
        payload = {**self._saved_meta, "cases": self._saved_cases}
        try:
            await asyncio.to_thread(write_json_atomic, self.path, payload, indent=2)
        except (OSError, TypeError) as exc:
            # SQLite stays authoritative; the next save or startup writes the mirror again.
            LOGGER.error("Failed saving %s: %s", self.path, exc)
