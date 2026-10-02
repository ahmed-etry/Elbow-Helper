"""Durable action runs, audit entries, and shared scheduling state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from elbow_helper.infrastructure.persistence import (
    SQLiteMigration,
    run_sqlite_migrations,
    sqlite_transaction,
)

from .codec import encode_record
from .schema import create_schema
from ..scheduled.store import ScheduledStore

ACTION_LOG_RETENTION_SECONDS = 90 * 24 * 60 * 60


class AgentActionRepository(ScheduledStore):
    """Persist each transition before an external change is attempted."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        with self.connect() as connection:
            run_sqlite_migrations(
                connection,
                (SQLiteMigration(1, "Agent action state", create_schema),),
                target_version=1,
            )

    def record_message(
        self,
        *,
        message_id: int,
        guild_id: int,
        channel_id: int,
        requester_id: int,
        now: float | None = None,
    ) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (
                message_id,
                guild_id,
                channel_id,
                requester_id,
            )
        ):
            raise ValueError("Invalid agent message")
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            connection.execute(
                """
                INSERT INTO agent_messages (
                    message_id, guild_id, channel_id, requester_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO NOTHING
            """,
                (
                    message_id,
                    guild_id,
                    channel_id,
                    requester_id,
                    time.time() if now is None else now,
                ),
            )

    def agent_message(
        self, *, message_id: int, guild_id: int, channel_id: int
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM agent_messages
                WHERE message_id=? AND guild_id=? AND channel_id=? AND deleted_at IS NULL
            """,
                (message_id, guild_id, channel_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def mark_message_deleted(
        self, *, message_id: int, guild_id: int, channel_id: int, now: float | None = None
    ) -> bool:
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            changed = connection.execute(
                """
                UPDATE agent_messages SET deleted_at=?
                WHERE message_id=? AND guild_id=? AND channel_id=? AND deleted_at IS NULL
            """,
                (time.time() if now is None else now, message_id, guild_id, channel_id),
            )
            return changed.rowcount == 1

    def create_run(
        self,
        *,
        guild_id: int,
        channel_id: int,
        request_message_id: int,
        requester_id: int,
        confirmer_id: int,
        steps: Sequence[Mapping[str, Any]],
        now: float | None = None,
    ) -> str:
        if (
            any(
                type(value) is not int or value <= 0
                for value in (
                    guild_id,
                    channel_id,
                    request_message_id,
                    requester_id,
                    confirmer_id,
                )
            )
            or not steps
        ):
            raise ValueError("Invalid action run")
        if requester_id != confirmer_id:
            raise ValueError("Only the requester may confirm")
        run_id = uuid4().hex
        current = time.time() if now is None else now
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            connection.execute(
                """
                INSERT INTO action_runs (
                    run_id, guild_id, channel_id, request_message_id,
                    requester_id, confirmer_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
                (
                    run_id,
                    guild_id,
                    channel_id,
                    request_message_id,
                    requester_id,
                    confirmer_id,
                    current,
                    current,
                ),
            )
            for index, step in enumerate(steps):
                connection.execute(
                    """
                    INSERT INTO action_steps (
                        run_id, step_index, action_name, action_label, action_class,
                        values_json, preview_json, before_json, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued')
                """,
                    (
                        run_id,
                        index,
                        step["name"],
                        step.get("label") or step["name"],
                        step["class"],
                        encode_record(step["values"]),
                        encode_record(step["preview"]),
                        encode_record(step.get("before")) if "before" in step else None,
                    ),
                )
        return run_id

    def claim(self, run_id: str, *, owner: str, version: int = 0, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            changed = connection.execute(
                """
                UPDATE action_runs SET status='running', version=version+1,
                    lease_owner=?, updated_at=?
                WHERE run_id=? AND status='queued' AND version=?
                    AND lease_owner IS NULL
            """,
                (owner, current, run_id, version),
            )
            return changed.rowcount == 1

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM action_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            steps = connection.execute(
                "SELECT * FROM action_steps WHERE run_id=? ORDER BY step_index",
                (run_id,),
            ).fetchall()
        return {**dict(row), "steps": [dict(step) for step in steps]}

    def start_step(self, run_id: str, index: int, *, owner: str, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            run = connection.execute(
                "SELECT * FROM action_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None or run["status"] != "running" or run["lease_owner"] != owner:
                return False
            if run["stop_requested"]:
                return False
            step = connection.execute(
                """
                SELECT * FROM action_steps WHERE run_id=? AND step_index=?
            """,
                (run_id, index),
            ).fetchone()
            if step is None or step["status"] != "queued":
                return False
            connection.execute(
                """
                UPDATE action_steps SET status='running', started_at=?
                WHERE run_id=? AND step_index=?
            """,
                (current, run_id, index),
            )
            connection.execute(
                """
                INSERT INTO action_log (
                    log_id, run_id, step_index, requester_id, confirmer_id,
                    action_name, action_label, action_class, targets_json, before_json,
                    outcome, executed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'started', ?)
            """,
                (
                    uuid4().hex,
                    run_id,
                    index,
                    run["requester_id"],
                    run["confirmer_id"],
                    step["action_name"],
                    step["action_label"],
                    step["action_class"],
                    step["values_json"],
                    step["before_json"],
                    current,
                ),
            )
            connection.execute(
                """
                UPDATE action_runs SET version=version+1, updated_at=?
                WHERE run_id=?
            """,
                (current, run_id),
            )
        return True

    def finish_step(
        self,
        run_id: str,
        index: int,
        *,
        owner: str,
        status: str,
        outcome: Mapping[str, Any],
        after: Any = None,
        now: float | None = None,
    ) -> bool:
        if status not in {"completed", "failed", "uncertain"}:
            raise ValueError("Invalid action outcome")
        current = time.time() if now is None else now
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            run = connection.execute(
                "SELECT status, lease_owner FROM action_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None or run["status"] != "running" or run["lease_owner"] != owner:
                return False
            changed = connection.execute(
                """
                UPDATE action_steps SET status=?, outcome_json=?, after_json=?, finished_at=?
                WHERE run_id=? AND step_index=? AND status='running'
            """,
                (
                    status,
                    encode_record(outcome),
                    encode_record(after) if after is not None else None,
                    current,
                    run_id,
                    index,
                ),
            )
            if changed.rowcount != 1:
                return False
            connection.execute(
                """
                UPDATE action_log SET outcome=?, after_json=?
                WHERE run_id=? AND step_index=?
            """,
                (status, encode_record(after) if after is not None else None, run_id, index),
            )
            connection.execute(
                """
                UPDATE action_runs SET version=version+1, updated_at=? WHERE run_id=?
            """,
                (current, run_id),
            )
        return True

    def set_step_values(
        self, run_id: str, index: int, *, owner: str, values: Mapping[str, Any]
    ) -> bool:
        encoded = encode_record(values)
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            run = connection.execute(
                "SELECT status, lease_owner FROM action_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None or run["status"] != "running" or run["lease_owner"] != owner:
                return False
            changed = connection.execute(
                """
                UPDATE action_steps SET values_json=?
                WHERE run_id=? AND step_index=? AND status='running'
            """,
                (encoded, run_id, index),
            )
            if changed.rowcount != 1:
                return False
            connection.execute(
                """
                UPDATE action_log SET targets_json=?
                WHERE run_id=? AND step_index=? AND outcome='started'
            """,
                (encoded, run_id, index),
            )
        return True

    def request_stop(self, run_id: str, *, requester_id: int) -> bool:
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            changed = connection.execute(
                """
                UPDATE action_runs SET stop_requested=1, version=version+1,
                    updated_at=? WHERE run_id=? AND requester_id=? AND status='running'
            """,
                (time.time(), run_id, requester_id),
            )
            return changed.rowcount == 1

    def finish_run(self, run_id: str, *, owner: str, status: str, now: float | None = None) -> bool:
        if status not in {"completed", "failed", "stopped"}:
            raise ValueError("Invalid run status")
        current = time.time() if now is None else now
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            changed = connection.execute(
                """
                UPDATE action_runs SET status=?, version=version+1,
                    lease_owner=NULL, updated_at=?
                WHERE run_id=? AND status='running' AND lease_owner=?
            """,
                (status, current, run_id, owner),
            )
            return changed.rowcount == 1

    def interrupt_incomplete(
        self, *, guild_id: int, now: float | None = None
    ) -> list[dict[str, Any]]:
        current = time.time() if now is None else now
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            rows = connection.execute(
                """
                SELECT * FROM action_runs WHERE guild_id=?
                    AND status IN ('queued', 'running')
                ORDER BY created_at
            """,
                (guild_id,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE action_runs SET status='interrupted', version=version+1,
                        lease_owner=NULL, updated_at=?
                    WHERE run_id=?
                """,
                    (current, row["run_id"]),
                )
                connection.execute(
                    """
                    UPDATE action_steps SET status='interrupted', finished_at=?
                    WHERE run_id=? AND status='running'
                """,
                    (current, row["run_id"]),
                )
                connection.execute(
                    """
                    UPDATE action_log SET outcome='interrupted'
                    WHERE run_id=? AND outcome='started'
                """,
                    (row["run_id"],),
                )
        return [self.run(row["run_id"]) for row in rows]

    def mark_reported(self, run_id: str, *, now: float | None = None) -> None:
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            connection.execute(
                """
                UPDATE action_runs SET reported_at=?
                WHERE run_id=? AND status='interrupted' AND reported_at IS NULL
            """,
                (time.time() if now is None else now, run_id),
            )

    def unreported_interruptions(self, *, guild_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id FROM action_runs WHERE guild_id=?
                    AND status='interrupted' AND reported_at IS NULL
                ORDER BY created_at
            """,
                (guild_id,),
            ).fetchall()
        return [self.run(row["run_id"]) for row in rows]

    def recent_log(
        self, *, requester_id: int, limit: int = 25, offset: int = 0
    ) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("Invalid action log limit")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM action_log WHERE requester_id=?
                ORDER BY executed_at DESC LIMIT ? OFFSET ?
            """,
                (requester_id, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def log_entry(self, log_id: str, *, requester_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM action_log WHERE log_id=? AND requester_id=?
            """,
                (log_id, requester_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def prune_log(self, *, now: float | None = None) -> int:
        cutoff = (time.time() if now is None else now) - ACTION_LOG_RETENTION_SECONDS
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            changed = connection.execute(
                "DELETE FROM action_log WHERE executed_at < ?",
                (cutoff,),
            )
            return changed.rowcount
