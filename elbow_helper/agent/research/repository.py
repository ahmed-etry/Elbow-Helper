"""Durable, bounded read-job state for explicit-channel Discord research."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import time
from typing import Sequence
from uuid import uuid4

from elbow_helper.infrastructure.persistence import (
    SQLiteMigration, run_sqlite_migrations, sqlite_connection,
    sqlite_transaction,
)
from .contracts import (
    DiscordResearchJob,
    JOB_IDLE_SECONDS,
    JOB_LEASE_SECONDS,
    JOB_STATUSES,
    MAX_RESEARCH_BATCH_JOBS,
    ResearchJobDefinition,
    ResearchJobScope,
    decode_research_job as _decode_job,
    finite_time as _finite_time,
    positive_int as _positive_int,
)


RESEARCH_JOB_RETRY_SECONDS = 30


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE research_jobs (
            job_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            conversation_root_id INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            source_channel_id INTEGER NOT NULL,
            query TEXT NOT NULL,
            author_id INTEGER,
            after_value TEXT,
            before_value TEXT,
            page_size INTEGER NOT NULL,
            cursor TEXT,
            status TEXT NOT NULL,
            messages_json TEXT NOT NULL,
            pages_completed INTEGER NOT NULL,
            total_results_estimate INTEGER,
            coverage_json TEXT,
            continuable INTEGER NOT NULL,
            cancellation_requested INTEGER NOT NULL,
            error_class TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            version INTEGER NOT NULL,
            lease_owner TEXT,
            lease_expires_at REAL
        )
    """)
    connection.execute(
        "CREATE INDEX research_jobs_expiry ON research_jobs(expires_at)"
    )


def _add_job_kind(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE research_jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'search'"
    )


from .transitions import ResearchTransitionStore
from .contracts import ResearchJobBusy, ResearchJobConflict


class ResearchJobRepository(ResearchTransitionStore):
    """Own the SQLite lifecycle and atomic transitions for read-only jobs."""

    def __init__(self, path: Path):
        self.path = path
        with self.connect() as connection:
            run_sqlite_migrations(
                connection,
                (
                    SQLiteMigration(1, "Discord research jobs", _create_schema),
                    SQLiteMigration(2, "Discord research job kinds", _add_job_kind),
                ),
                target_version=2,
            )
            connection.execute("SELECT * FROM research_jobs LIMIT 0")

    def connect(self):
        return sqlite_connection(
            self.path, timeout_seconds=30, busy_timeout_ms=30_000,
            synchronous="FULL",
        )

    def create(
        self,
        *,
        guild_id: int,
        conversation_root_id: int,
        requester_id: int,
        source_channel_id: int,
        query: str,
        author_id: int | None,
        after: str | None,
        before: str | None,
        page_size: int,
        kind: str = "search",
        now: float | None = None,
    ) -> DiscordResearchJob:
        return self.create_many(
            guild_id=guild_id,
            conversation_root_id=conversation_root_id,
            requester_id=requester_id,
            definitions=(ResearchJobDefinition(
                source_channel_id=source_channel_id,
                query=query,
                author_id=author_id,
                after=after,
                before=before,
                page_size=page_size,
                kind=kind,
            ),),
            now=now,
        )[0]

    def create_many(
        self,
        *,
        guild_id: int,
        conversation_root_id: int,
        requester_id: int,
        definitions: Sequence[ResearchJobDefinition],
        now: float | None = None,
    ) -> tuple[DiscordResearchJob, ...]:
        """Atomically create a small set of independently executable jobs."""
        for value in (
            guild_id, conversation_root_id, requester_id,
        ):
            _positive_int(value)
        if (
            not isinstance(definitions, Sequence)
            or not 1 <= len(definitions) <= MAX_RESEARCH_BATCH_JOBS
            or any(not isinstance(item, ResearchJobDefinition) for item in definitions)
            or len({item.source_channel_id for item in definitions}) != len(definitions)
        ):
            raise ValueError("Invalid research job batch")
        current = _finite_time(time.time() if now is None else now)
        with self.connect() as connection, sqlite_transaction(
            connection, immediate=True,
        ):
            job_ids = tuple(uuid4().hex for _ in definitions)
            for job_id, definition in zip(job_ids, definitions):
                connection.execute(
                    """INSERT INTO research_jobs (
                        job_id, guild_id, conversation_root_id, requester_id,
                        source_channel_id, query, author_id, after_value,
                        before_value, page_size, cursor, status, messages_json,
                        pages_completed, total_results_estimate, coverage_json,
                        continuable, cancellation_requested, error_class,
                        created_at, updated_at, expires_at, version, lease_owner,
                        lease_expires_at, kind
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'queued', '[]', 0,
                        NULL, NULL, 1, 0, NULL, ?, ?, ?, 0, NULL, NULL, ?
                    )""",
                    (
                        job_id, guild_id, conversation_root_id, requester_id,
                        definition.source_channel_id, definition.query,
                        definition.author_id, definition.after,
                        definition.before, definition.page_size,
                        current, current, current + JOB_IDLE_SECONDS,
                        definition.kind,
                    ),
                )
            rows = connection.execute(
                f"SELECT * FROM research_jobs WHERE job_id IN "
                f"({','.join('?' for _ in job_ids)})",
                job_ids,
            ).fetchall()
        jobs = {}
        for row in rows:
            job = _decode_job(row)
            jobs[job.job_id] = job
        if jobs.keys() != set(job_ids):
            raise RuntimeError("Research jobs were not created")
        return tuple(jobs[job_id] for job_id in job_ids)

    def peek(
        self,
        job_id: str,
        *,
        guild_id: int,
        conversation_root_id: int,
        now: float | None = None,
        touch: bool = False,
    ) -> DiscordResearchJob | None:
        current = _finite_time(time.time() if now is None else now)
        with self.connect() as connection, sqlite_transaction(
            connection, immediate=True,
        ):
            row = connection.execute(
                """SELECT * FROM research_jobs
                   WHERE job_id = ? AND guild_id = ? AND conversation_root_id = ?""",
                (job_id, guild_id, conversation_root_id),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= current:
                connection.execute(
                    "DELETE FROM research_jobs WHERE job_id = ?", (job_id,),
                )
                return None
            if touch:
                connection.execute(
                    """UPDATE research_jobs SET updated_at=?, expires_at=?,
                       version=version+1 WHERE job_id=?""",
                    (current, current + JOB_IDLE_SECONDS, job_id),
                )
                row = connection.execute(
                    "SELECT * FROM research_jobs WHERE job_id=?", (job_id,),
                ).fetchone()
        return _decode_job(row)

    def scope(
        self,
        job_id: str,
        *,
        guild_id: int,
        conversation_root_id: int,
        now: float | None = None,
    ) -> ResearchJobScope | None:
        """Read authorization metadata without loading retained message content."""
        current = _finite_time(time.time() if now is None else now)
        with self.connect() as connection, sqlite_transaction(
            connection, immediate=True,
        ):
            row = connection.execute(
                """SELECT job_id, guild_id, conversation_root_id, requester_id,
                          source_channel_id, status, expires_at
                   FROM research_jobs
                   WHERE job_id=? AND guild_id=? AND conversation_root_id=?""",
                (job_id, guild_id, conversation_root_id),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= current:
                connection.execute(
                    "DELETE FROM research_jobs WHERE job_id=?", (job_id,),
                )
                return None
        if row["status"] not in JOB_STATUSES:
            raise ValueError("Invalid persisted research job status")
        return ResearchJobScope(
            job_id=row["job_id"], guild_id=_positive_int(row["guild_id"]),
            conversation_root_id=_positive_int(row["conversation_root_id"]),
            requester_id=_positive_int(row["requester_id"]),
            source_channel_id=_positive_int(row["source_channel_id"]),
            status=row["status"],
        )

    def conversation_scopes(
        self,
        *,
        guild_id: int,
        conversation_root_id: int,
        limit: int = 21,
        now: float | None = None,
    ) -> tuple[ResearchJobScope, ...]:
        """List bounded unexpired authorization metadata without touching jobs."""
        _positive_int(guild_id)
        _positive_int(conversation_root_id)
        if type(limit) is not int or not 1 <= limit <= 26:
            raise ValueError("Invalid research job inventory limit")
        current = _finite_time(time.time() if now is None else now)
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT job_id, guild_id, conversation_root_id, requester_id,
                          source_channel_id, status
                   FROM research_jobs
                   WHERE guild_id=? AND conversation_root_id=? AND expires_at>?
                   ORDER BY updated_at DESC, created_at DESC, job_id
                   LIMIT ?""",
                (guild_id, conversation_root_id, current, limit),
            ).fetchall()
        if any(row["status"] not in JOB_STATUSES for row in rows):
            raise ValueError("Invalid persisted research job status")
        return tuple(ResearchJobScope(
            job_id=row["job_id"], guild_id=_positive_int(row["guild_id"]),
            conversation_root_id=_positive_int(row["conversation_root_id"]),
            requester_id=_positive_int(row["requester_id"]),
            source_channel_id=_positive_int(row["source_channel_id"]),
            status=row["status"],
        ) for row in rows)

    def claimable_scopes(
        self, *, guild_id: int, limit: int = 4,
        now: float | None = None, after_job_id: str | None = None,
    ) -> tuple[ResearchJobScope, ...]:
        """List bounded work eligible for a lease without loading message data."""
        _positive_int(guild_id)
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("Invalid research job claimable limit")
        if after_job_id is not None and (
            not isinstance(after_job_id, str) or len(after_job_id) != 32
            or any(character not in "0123456789abcdef"
                   for character in after_job_id)
        ):
            raise ValueError("Invalid research job scheduling cursor")
        current = _finite_time(time.time() if now is None else now)
        cursor = after_job_id or ""
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT job_id, guild_id, conversation_root_id, requester_id,
                          source_channel_id, status
                   FROM research_jobs
                   WHERE guild_id=? AND expires_at>? AND continuable=1
                     AND job_id>?
                     AND cancellation_requested=0
                     AND (
                       (status!='waiting' AND error_class IS NULL)
                       OR updated_at<=?
                     )
                     AND (
                       status IN ('queued', 'partial', 'waiting')
                       OR (status='running' AND lease_expires_at<=?)
                     )
                   ORDER BY job_id
                   LIMIT ?""",
                (
                    guild_id, current, cursor,
                    current - RESEARCH_JOB_RETRY_SECONDS,
                    current, limit,
                ),
            ).fetchall()
        return tuple(ResearchJobScope(
            job_id=row["job_id"], guild_id=_positive_int(row["guild_id"]),
            conversation_root_id=_positive_int(row["conversation_root_id"]),
            requester_id=_positive_int(row["requester_id"]),
            source_channel_id=_positive_int(row["source_channel_id"]),
            status=row["status"],
        ) for row in rows)

    def claim(
        self,
        job_id: str,
        *,
        guild_id: int,
        conversation_root_id: int,
        lease_owner: str,
        expected_version: int | None = None,
        now: float | None = None,
    ) -> DiscordResearchJob | None:
        current = _finite_time(time.time() if now is None else now)
        if not isinstance(lease_owner, str) or not lease_owner or len(lease_owner) > 64:
            raise ValueError("Invalid research job lease owner")
        if expected_version is not None and (
            type(expected_version) is not int or expected_version < 0
        ):
            raise ValueError("Invalid expected research job version")
        with self.connect() as connection, sqlite_transaction(
            connection, immediate=True,
        ):
            row = connection.execute(
                """SELECT * FROM research_jobs
                   WHERE job_id = ? AND guild_id = ? AND conversation_root_id = ?""",
                (job_id, guild_id, conversation_root_id),
            ).fetchone()
            if row is None or row["expires_at"] <= current:
                if row is not None:
                    connection.execute(
                        "DELETE FROM research_jobs WHERE job_id = ?", (job_id,),
                    )
                return None
            job = _decode_job(row)
            if expected_version is not None and job.version != expected_version:
                raise ResearchJobConflict("Research job version changed")
            if job.cancellation_requested or job.status == "cancelled":
                connection.execute(
                    """UPDATE research_jobs
                       SET status='cancelled', continuable=0, lease_owner=NULL,
                           lease_expires_at=NULL, updated_at=?, version=version+1
                       WHERE job_id=?""",
                    (current, job_id),
                )
                row = connection.execute(
                    "SELECT * FROM research_jobs WHERE job_id=?", (job_id,),
                ).fetchone()
                return _decode_job(row)
            if not job.continuable:
                return job
            if (
                job.status == "running"
                and job.lease_expires_at is not None
                and job.lease_expires_at > current
            ):
                raise ResearchJobBusy("Research job already has a live worker")
            updated = connection.execute(
                """UPDATE research_jobs
                   SET status='running', lease_owner=?, lease_expires_at=?,
                       updated_at=?, expires_at=?, version=version+1
                   WHERE job_id=? AND version=?""",
                (
                    lease_owner, current + JOB_LEASE_SECONDS, current,
                    current + JOB_IDLE_SECONDS, job_id, job.version,
                ),
            )
            if updated.rowcount != 1:
                raise ResearchJobConflict("Research job claim conflicted")
            row = connection.execute(
                "SELECT * FROM research_jobs WHERE job_id=?", (job_id,),
            ).fetchone()
            return _decode_job(row)


__all__ = [
    "DiscordResearchJob", "ResearchJobBusy", "ResearchJobConflict",
    "ResearchJobRepository", "ResearchJobScope",
]

__all__ = ["ResearchJobRepository", "ResearchJobBusy", "ResearchJobConflict"]
