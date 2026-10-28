"""Serialized family-record writer. All disk work stays on worker threads."""

import asyncio
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
import time

from elbow_helper.infrastructure.persistence.migrations import run_sqlite_migrations
from elbow_helper.infrastructure.persistence.sqlite import sqlite_connection, sqlite_transaction
from elbow_helper.infrastructure.persistence.versioned import validate_sqlite_shape

from .schema import MIGRATIONS, SCHEMA_VERSION, create_schema
from .projections import canonical_json


def now_us() -> int:
    return time.time_ns() // 1000


def _queued(collector):
    """High-volume sources queue on checkpoints instead of item rows."""
    if collector == "players":
        from . import player_state
        return player_state
    if collector == "wars":
        from . import war_state
        return war_state
    from . import source_state
    if collector in source_state.COLLECTORS:
        return source_state
    return None


class FamilyStore:
    """One worker-thread serialization boundary for this database's writes.

    A thread lock also protects a finishing transaction if its async caller is
    cancelled. No persistent connection crosses threads or survives shutdown.
    """

    def __init__(self, path: Path):
        self.path = path
        self._writer = Lock()
        self._ready = False

    async def call(self, operation, *args, **kwargs):
        return await asyncio.to_thread(self._call, operation, args, kwargs)

    def _call(self, operation, args, kwargs):
        with self._writer, self._connection() as connection:
            return operation(connection, *args, **kwargs)

    @contextmanager
    def _connection(self):
        if self._ready and not self.path.is_file():
            raise FileNotFoundError(self.path)
        with sqlite_connection(self.path, timeout_seconds=30, foreign_keys=True) as connection:
            if not self._ready:
                run_sqlite_migrations(connection, MIGRATIONS, target_version=SCHEMA_VERSION)
                validate_sqlite_shape(connection, create_schema)
                connection.execute("PRAGMA journal_mode=WAL")
                self._recover(connection)
                self._ready = True
            yield connection

    async def initialize(self) -> None:
        await self.call(lambda connection: None)

    @staticmethod
    def _recover(connection):
        with sqlite_transaction(connection, immediate=True):
            runs = connection.execute("SELECT id FROM collection_runs WHERE finished_at IS NULL").fetchall()
            for run in runs:
                FamilyStore._fail_pending(connection, run["id"], "interrupted", now_us())
                FamilyStore._finish(connection, run["id"], now_us(), interrupted=True)

    async def begin_run(self, collector: str, scope_kind: str, specs: list[tuple[str, str]], at: int) -> int:
        return await self.call(self._begin_run, collector, scope_kind, specs, at)

    @staticmethod
    def _begin_run(connection, collector, scope_kind, specs, at):
        with sqlite_transaction(connection, immediate=True):
            run = connection.execute(
                "INSERT INTO collection_runs(collector,scope_kind,scheduled_at,started_at,status,expected) "
                "VALUES (?,?,?,?, 'running',?)", (collector, scope_kind, at, at, len(specs)),
            ).lastrowid
            if state := _queued(collector):
                state.queue(connection, run, specs, at)
                return run
            connection.executemany(
                "INSERT INTO collection_items(run_id,ordinal,source_kind,scope_id,status,started_at) "
                "VALUES (?,?,?,?, 'pending',?)",
                [(run, ordinal, kind, scope, at) for ordinal, (kind, scope) in enumerate(specs)],
            )
            return run

    @staticmethod
    def item(connection, run: int, ordinal: int):
        return connection.execute(
            "SELECT * FROM collection_items WHERE run_id=? AND ordinal=?", (run, ordinal),
        ).fetchone()

    @staticmethod
    def previous_good(connection, kind: str, scope: str) -> int | None:
        return connection.execute(
            "SELECT MAX(fetched_at) FROM collection_items WHERE source_kind=? AND scope_id=? "
            "AND status IN ('ok','empty')", (kind, scope),
        ).fetchone()[0]

    @staticmethod
    def outcome(connection, item: int, status: str, at: int, *, error=None, response=None,
                count=None, written=0, complete=True):
        connection.execute(
            "UPDATE collection_items SET status=?,error_code=?,http_status=?,attempts=?,"
            "fetched_at=?,committed_at=?,returned_count=?,written_count=?,completeness_json=? WHERE id=?",
            (status, error, response.status if response else None, response.attempts if response else None,
             at, now_us(), count, written, canonical_json({"complete": complete}), item),
        )

    async def finish(self, run: int, *, error: str | None = None) -> dict:
        return await self.call(self._finish_run, run, error)

    async def requested(self, run: int, ordinal: int) -> None:
        await self.call(self._requested, run, ordinal)

    @staticmethod
    def _requested(connection, run, ordinal):
        with sqlite_transaction(connection, immediate=True):
            collector = connection.execute("SELECT collector FROM collection_runs WHERE id=?", (run,)).fetchone()[0]
            if state := _queued(collector):
                state.requested(connection, run, ordinal, now_us())
                return
            connection.execute(
                "UPDATE collection_items SET status='running',started_at=? "
                "WHERE run_id=? AND ordinal=? AND status='pending'", (now_us(), run, ordinal),
            )

    @staticmethod
    def _fail_pending(connection, run, error, at):
        connection.execute(
            "UPDATE collection_items SET status='failed',error_code=?,committed_at=? "
            "WHERE run_id=? AND status='running'", (error, at, run),
        )
        connection.execute(
            "UPDATE collection_items SET status='skipped',error_code=?,committed_at=? "
            "WHERE run_id=? AND status='pending'", (error, at, run),
        )

    @staticmethod
    def _finish_run(connection, run, error):
        with sqlite_transaction(connection, immediate=True):
            FamilyStore._fail_pending(connection, run, error or "unfinished", now_us())
            return FamilyStore._finish(connection, run, now_us(), interrupted=error == "interrupted")

    @staticmethod
    def _finish(connection, run, at, *, interrupted=False):
        collector = connection.execute("SELECT collector FROM collection_runs WHERE id=?", (run,)).fetchone()[0]
        if state := _queued(collector):
            return state.finish(connection, run, at, interrupted=interrupted)
        counts = {row["status"]: row["n"] for row in connection.execute(
            "SELECT status,COUNT(*) AS n FROM collection_items WHERE run_id=? GROUP BY status", (run,),
        )}
        ok, empty, failed, skipped = (counts.get(key, 0) for key in ("ok", "empty", "failed", "skipped"))
        written = connection.execute(
            "SELECT COALESCE(SUM(written_count),0) FROM collection_items WHERE run_id=?", (run,),
        ).fetchone()[0]
        status = ("interrupted" if interrupted else "partial" if failed + skipped and ok + empty else
                  "failed" if failed + skipped else "empty" if not ok else "ok")
        connection.execute(
            "UPDATE collection_runs SET finished_at=?,status=?,attempted=?,succeeded=?,empty=?,"
            "failed=?,skipped=?,rows_written=? WHERE id=?",
            (at, status, ok + empty + failed, ok, empty, failed, skipped, written, run),
        )
        return {"status": status, "failed": failed, "succeeded": ok, "empty": empty}

    async def due(self, collector: str, interval_seconds: int, at: int) -> bool:
        return await self.call(self._due, collector, interval_seconds, at)

    @staticmethod
    def _due(connection, collector, seconds, at):
        previous = connection.execute(
            "SELECT MAX(scheduled_at) FROM collection_runs WHERE collector=?", (collector,),
        ).fetchone()[0]
        return previous is None or at >= previous + seconds * 1_000_000
