"""One serialized worker boundary for atomic workflow state and event writes."""

import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
from threading import Lock

from elbow_helper.infrastructure.persistence.migrations import run_sqlite_migrations
from elbow_helper.infrastructure.persistence.sqlite import sqlite_connection, sqlite_transaction
from elbow_helper.infrastructure.persistence.versioned import validate_sqlite_shape

from .records import Event, KINDS, now_us, record
from .schema import MIGRATIONS, SCHEMA_VERSION, create_schema


async def finish_on_cancel(awaitable):
    """Let a started save publish its memory result before cancellation escapes."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


def load_state(connection, workflow):
    row = connection.execute("SELECT state_json FROM workflow_state WHERE workflow=?", (workflow,)).fetchone()
    return json.loads(row[0]) if row else None


def save_state(connection, workflow, state):
    if workflow not in KINDS or not connection.in_transaction:
        raise ValueError("Workflow state requires a known workflow and transaction")
    connection.execute(
        "INSERT INTO workflow_state(workflow,state_json,imported_at) VALUES (?,?,?) "
        "ON CONFLICT(workflow) DO UPDATE SET state_json=excluded.state_json",
        (workflow, json.dumps(state, sort_keys=True, separators=(",", ":")), now_us()))


class EventsStore:
    def __init__(self, path: Path):
        self.path = path
        self._writer = Lock()
        self._ready = False
        self._deletion_handlers = {}

    def register_member_deletion(self, workflow, handler):
        self._deletion_handlers[workflow] = handler

    def unregister_member_deletion(self, workflow):
        self._deletion_handlers.pop(workflow, None)

    async def call(self, operation, *args, **kwargs):
        return await asyncio.to_thread(self._call, operation, args, kwargs)

    def _call(self, operation, args, kwargs):
        with self._writer, self._connection() as connection:
            with sqlite_transaction(connection, immediate=True):
                return operation(connection, *args, **kwargs)

    @contextmanager
    def _connection(self):
        if self._ready and not self.path.is_file():
            raise FileNotFoundError(self.path)
        with sqlite_connection(self.path, timeout_seconds=30, synchronous="FULL") as connection:
            if not self._ready:
                run_sqlite_migrations(connection, MIGRATIONS, target_version=SCHEMA_VERSION)
                validate_sqlite_shape(connection, create_schema)
                connection.execute("PRAGMA journal_mode=WAL")
                self._ready = True
            yield connection

    async def initialize(self):
        await self.call(lambda connection: None)

    async def read(self, *, workflow=None, kind=None, subject=None, since=None, until=None, case=None):
        """Read dated events in occurrence order (inclusive bounds)."""
        def query(connection):
            filters = {"workflow": workflow, "kind": kind, "subject_discord_id": subject,
                       "case_key": case}
            clauses, params = [], []
            for column, value in filters.items():
                if value is not None:
                    clauses.append(f"{column}=?")
                    params.append(value)
            for operator, value in ((">=", since), ("<=", until)):
                if value is not None:
                    clauses.append(f"occurred_at{operator}?")
                    params.append(value)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            return [dict(row) | {"detail": json.loads(row["detail_json"])} for row in connection.execute(
                "SELECT * FROM workflow_events" + where + " ORDER BY occurred_at,id", params)]
        return await self.call(query)

    async def delete_member(self, discord_id):
        """Delete subject/actor history and identifiable open workflow state."""
        if type(discord_id) is not int or discord_id <= 0:
            raise ValueError("Invalid member identifier")

        def delete(connection):
            count = connection.execute(
                "DELETE FROM workflow_events WHERE subject_discord_id=? OR actor_discord_id=?",
                (discord_id, discord_id)).rowcount
            for workflow in KINDS:
                state = load_state(connection, workflow)
                if state is None:
                    continue
                if workflow == "transfers":
                    for queue in state["clans"].values():
                        queue["pending"] = [row for row in queue["pending"] if row["user_id"] != discord_id]
                elif workflow == "support":
                    state = {key: row for key, row in state.items()
                             if row.get("owner") != discord_id
                             and str(row.get("created_by")) != str(discord_id)}
                elif workflow == "hibernation":
                    state.pop(str(discord_id), None)
                    state.get("_fallback_threads", {}).pop(str(discord_id), None)
                    for key, info in state.items():
                        if key.startswith("_"):
                            continue
                        for marker in ("hibernation_progress", "reactivation_progress"):
                            progress = info.get(marker, {})
                            if progress.get("actor_discord_id") == discord_id:
                                progress["actor_discord_id"] = None
                elif workflow == "recruitment":
                    for part in ("trials", "reminders"):
                        state[part] = {key: row for key, row in state[part].items()
                                       if str(row.get("applicant_id")) != str(discord_id)}
                    for row in state["reminders"].values():
                        if row.get("resolved_by") == discord_id:
                            row["resolved_by"] = None
                else:
                    state["prompts"] = {key: row for key, row in state["prompts"].items()
                                        if row.get("subject_discord_id") != discord_id}
                save_state(connection, workflow, state)
            return count
        async def complete():
            count = await self.call(delete)
            for handler in tuple(self._deletion_handlers.values()):
                await handler(discord_id)
            return count
        return await finish_on_cancel(complete())


__all__ = ["Event", "EventsStore", "finish_on_cancel", "load_state", "now_us", "record", "save_state"]
