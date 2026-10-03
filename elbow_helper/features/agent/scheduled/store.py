"""Saved requests, watchers, and timezones on the shared action database."""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import sqlite3
import time
from typing import Any
from uuid import uuid4

from elbow_helper.infrastructure.persistence import sqlite_connection, sqlite_transaction
from ..actions.codec import encode_record


class ScheduledStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self):
        return sqlite_connection(
            self.path,
            timeout_seconds=30,
            busy_timeout_ms=30_000,
            synchronous="FULL",
        )

    def create_standing(
        self,
        *,
        kind: str,
        guild_id: int,
        requester_id: int,
        destination_channel_id: int,
        rule: Mapping[str, Any],
        next_at: float,
        now: float | None = None,
    ) -> str:
        table, key, due = self._standing_columns(kind)
        if (
            any(
                type(value) is not int or value <= 0
                for value in (
                    guild_id,
                    requester_id,
                    destination_channel_id,
                )
            )
            or next_at <= 0
        ):
            raise ValueError("Invalid standing rule")
        identifier = uuid4().hex
        current = time.time() if now is None else now
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            connection.execute(
                f"""
                INSERT INTO {table} ({key}, guild_id, requester_id,
                    destination_channel_id, rule_json, {due}, status,
                    created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
                (
                    identifier,
                    guild_id,
                    requester_id,
                    destination_channel_id,
                    encode_record(rule),
                    next_at,
                    current,
                    current,
                ),
            )
        return identifier

    @staticmethod
    def _standing_columns(kind: str) -> tuple[str, str, str]:
        if kind == "request":
            return "saved_requests", "request_id", "next_run_at"
        if kind == "watcher":
            return "watchers", "watcher_id", "next_check_at"
        raise ValueError("Invalid standing rule kind")

    def standing(
        self, *, kind: str, identifier: str, requester_id: int | None = None
    ) -> dict[str, Any] | None:
        table, key, _ = self._standing_columns(kind)
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT * FROM {table} WHERE {key}=?"
                + (" AND requester_id=?" if requester_id is not None else ""),
                (identifier,) if requester_id is None else (identifier, requester_id),
            ).fetchone()
        return self._standing_record(row) if row is not None else None

    def list_standing(self, *, requester_id: int, kind: str | None = None) -> list[dict[str, Any]]:
        kinds = (kind,) if kind is not None else ("request", "watcher")
        records = []
        with self.connect() as connection:
            for selected in kinds:
                table, _, _ = self._standing_columns(selected)
                rows = connection.execute(
                    f"SELECT * FROM {table} WHERE requester_id=? AND status!='cancelled' "
                    "ORDER BY created_at DESC",
                    (requester_id,),
                ).fetchall()
                records.extend({**self._standing_record(row), "kind": selected} for row in rows)
        return records

    @staticmethod
    def _standing_record(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["rule"] = json.loads(value.pop("rule_json"))
        if "last_result_json" in value:
            last_result = value.pop("last_result_json")
            value["last_result"] = json.loads(last_result) if last_result else None
        return value

    def set_standing_status(
        self,
        *,
        kind: str,
        identifier: str,
        requester_id: int,
        status: str,
        now: float | None = None,
    ) -> bool:
        if status not in {"active", "paused", "cancelled"}:
            raise ValueError("Invalid standing rule status")
        table, key, _ = self._standing_columns(kind)
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            changed = connection.execute(
                f"""
                UPDATE {table} SET status=?, version=version+1, updated_at=?
                WHERE {key}=? AND requester_id=? AND status IN ('active', 'paused')
            """,
                (status, time.time() if now is None else now, identifier, requester_id),
            )
            return changed.rowcount == 1

    def replace_standing(
        self,
        *,
        kind: str,
        identifier: str,
        requester_id: int,
        rule: Mapping[str, Any],
        destination_channel_id: int,
        next_at: float,
        expected_version: int,
        now: float | None = None,
    ) -> bool:
        table, key, due = self._standing_columns(kind)
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            changed = connection.execute(
                f"""
                UPDATE {table} SET rule_json=?, destination_channel_id=?,
                    {due}=?, status='active', version=version+1, updated_at=?
                WHERE {key}=? AND requester_id=? AND version=? AND status!='cancelled'
                    AND lease_owner IS NULL
            """,
                (
                    encode_record(rule),
                    destination_channel_id,
                    next_at,
                    time.time() if now is None else now,
                    identifier,
                    requester_id,
                    expected_version,
                ),
            )
            return changed.rowcount == 1

    def due_standing(
        self, *, kind: str, guild_id: int, now: float | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        table, _, due = self._standing_columns(kind)
        current = time.time() if now is None else now
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM {table} WHERE guild_id=? AND status='active'
                    AND {due}<=? AND lease_owner IS NULL
                ORDER BY {due} LIMIT ?
            """,
                (guild_id, current, limit),
            ).fetchall()
        return [self._standing_record(row) for row in rows]

    def recover_standing_leases(
        self, *, guild_id: int, now: float | None = None
    ) -> list[dict[str, Any]]:
        current = time.time() if now is None else now
        interrupted = []
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            for kind in ("request", "watcher"):
                table, key, _ = self._standing_columns(kind)
                rows = connection.execute(
                    f"""
                    SELECT * FROM {table} WHERE guild_id=? AND lease_owner IS NOT NULL
                """,
                    (guild_id,),
                ).fetchall()
                for row in rows:
                    connection.execute(
                        f"""
                        UPDATE {table} SET status='paused', lease_owner=NULL,
                            version=version+1, updated_at=?
                        WHERE {key}=? AND lease_owner=?
                    """,
                        (current, row[key], row["lease_owner"]),
                    )
                    interrupted.append({**self._standing_record(row), "kind": kind})
        return interrupted

    def claim_standing(
        self, *, kind: str, identifier: str, version: int, owner: str, now: float | None = None
    ) -> bool:
        table, key, due = self._standing_columns(kind)
        current = time.time() if now is None else now
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            changed = connection.execute(
                f"""
                UPDATE {table} SET lease_owner=?,
                    version=version+1, updated_at=?
                WHERE {key}=? AND version=? AND status='active'
                    AND {due}<=? AND lease_owner IS NULL
            """,
                (owner, current, identifier, version, current),
            )
            return changed.rowcount == 1

    def finish_standing(
        self,
        *,
        kind: str,
        identifier: str,
        owner: str,
        next_at: float | None,
        status: str = "active",
        last_result: Any = None,
        holding: bool | None = None,
        notice_sent: bool | None = None,
        now: float | None = None,
    ) -> bool:
        if status not in {"active", "paused", "cancelled", "completed"}:
            raise ValueError("Invalid standing rule status")
        table, key, due = self._standing_columns(kind)
        parts = [f"{due}=?", "status=CASE WHEN status IN ('paused', 'cancelled') THEN status ELSE ? END",
                 "lease_owner=NULL", "version=version+1", "updated_at=?"]
        current = time.time() if now is None else now
        values: list[Any] = [next_at, status, current]
        if notice_sent is not None:
            parts.append("notice_sent=?")
            values.append(int(notice_sent))
        if kind == "watcher":
            parts += ["last_result_json=?", "holding=?"]
            values += [
                encode_record(last_result) if last_result is not None else None,
                int(bool(holding)),
            ]
        values += [identifier, owner]
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            changed = connection.execute(
                f"UPDATE {table} SET {', '.join(parts)} WHERE {key}=? AND lease_owner=?",
                values,
            )
            return changed.rowcount == 1

    def release_standing(self, *, kind: str, identifier: str, owner: str) -> bool:
        table, key, _ = self._standing_columns(kind)
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            changed = connection.execute(
                f"UPDATE {table} SET lease_owner=NULL, version=version+1, updated_at=? "
                f"WHERE {key}=? AND lease_owner=?",
                (time.time(), identifier, owner),
            )
            return changed.rowcount == 1

    def member_timezone(self, member_id: int) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT timezone_name FROM member_timezones WHERE member_id=?",
                (member_id,),
            ).fetchone()
        return row[0] if row is not None else None

    def set_member_timezone(self, member_id: int, name: str) -> None:
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            connection.execute(
                """
                INSERT INTO member_timezones(member_id, timezone_name, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(member_id) DO UPDATE SET timezone_name=excluded.timezone_name,
                    updated_at=excluded.updated_at
            """,
                (member_id, name, time.time()),
            )
