"""Durable, bounded read-job state for explicit-channel Discord research."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import time
from typing import Mapping, Sequence
from uuid import uuid4

from elbow_helper.infrastructure.persistence import (
    SQLiteMigration, run_sqlite_migrations, sqlite_connection,
    sqlite_transaction,
)
from .contracts import (
    DiscordResearchJob, JOB_IDLE_SECONDS, JOB_LEASE_SECONDS,
    JOB_STATUSES, MAX_JOB_COVERAGE_BYTES, MAX_JOB_MESSAGES,
    MAX_JOB_MESSAGES_BYTES, MAX_JOB_PAGES,
    MAX_RESEARCH_BATCH_JOBS, ResearchJobDefinition, ResearchJobScope,
    bounded_json as _bounded_json, decode_research_job as _decode_job,
    finite_time as _finite_time, positive_int as _positive_int,
    validate_research_coverage, validate_research_message,
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


class ResearchJobBusy(RuntimeError):
    """A live worker currently owns this job."""


class ResearchJobConflict(RuntimeError):
    """A job changed after the caller claimed it."""


class ResearchJobRepository:
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

    def checkpoint_page(
        self,
        claimed: DiscordResearchJob,
        *,
        lease_owner: str,
        page_messages: Sequence[Mapping[str, Any]],
        coverage: Mapping[str, Any],
        now: float | None = None,
    ) -> DiscordResearchJob:
        current = _finite_time(time.time() if now is None else now)
        validated = tuple(
            validate_research_message(dict(value)) for value in page_messages
        )
        validated_coverage = validate_research_coverage(
            dict(coverage), kind=claimed.kind,
        )
        if any(
            message["channel_id"] != claimed.source_channel_id
            or (
                claimed.author_id is not None
                and message["author_id"] != claimed.author_id
            )
            for message in validated
        ):
            raise ValueError("Research page escaped its persisted source scope")
        if claimed.kind == "history":
            message_ids = [message["message_id"] for message in validated]
            after_id = validated_coverage["window_after_message_id"]
            page_before_id = validated_coverage["page_before_message_id"]
            if (
                validated_coverage["requested_after"] != claimed.after
                or validated_coverage["requested_before"] != claimed.before
                or any(
                    not after_id < message_id < page_before_id
                    for message_id in message_ids
                )
                or any(
                    current <= following
                    for current, following in zip(message_ids, message_ids[1:])
                )
                or validated_coverage["returned_messages"] != len(validated)
            ):
                raise ValueError("History page escaped its persisted period")
        elif (
            validated_coverage.get("returned_accessible_matches") is not None
            and validated_coverage["returned_accessible_matches"]
            != len(validated)
        ):
            raise ValueError("Search coverage does not match its retained page")
        next_cursor = validated_coverage["next_cursor"]
        if claimed.kind == "search":
            total = validated_coverage["total_results_estimate"]
            deep = validated_coverage["deep_historical_indexing"]
            provider_limited = validated_coverage["offset_limit_reached"]
            reached_end = validated_coverage["reached_current_indexed_end"]
        else:
            total = None
            deep = False
            provider_limited = False
            reached_end = validated_coverage["reached_requested_start"]
        with self.connect() as connection, sqlite_transaction(
            connection, immediate=True,
        ):
            row = connection.execute(
                "SELECT * FROM research_jobs WHERE job_id = ?", (claimed.job_id,),
            ).fetchone()
            if row is None:
                raise ResearchJobConflict("Research job no longer exists")
            current_job = _decode_job(row)
            if current_job.cancellation_requested:
                connection.execute(
                    """UPDATE research_jobs SET status='cancelled', continuable=0,
                       lease_owner=NULL, lease_expires_at=NULL, updated_at=?,
                       version=version+1 WHERE job_id=?""",
                    (current, claimed.job_id),
                )
                row = connection.execute(
                    "SELECT * FROM research_jobs WHERE job_id=?",
                    (claimed.job_id,),
                ).fetchone()
                result = _decode_job(row)
                if result is None:
                    raise ResearchJobConflict("Cancelled research job disappeared")
                return result
            if (
                current_job.version != claimed.version
                or current_job.status != "running"
                or current_job.lease_owner != lease_owner
                or current_job.kind != claimed.kind
            ):
                raise ResearchJobConflict("Research job checkpoint conflicted")
            if claimed.kind == "history" and current_job.coverage is not None:
                previous = current_job.coverage
                if (
                    any(
                        validated_coverage[key] != previous[key]
                        for key in (
                            "requested_after", "requested_before",
                            "window_after_message_id",
                            "snapshot_before_message_id",
                        )
                    )
                    or validated_coverage["page_before_message_id"]
                    >= previous["page_before_message_id"]
                ):
                    raise ValueError("History checkpoint did not advance its window")
            existing = list(current_job.messages)
            if (
                claimed.kind == "history"
                and existing
                and validated
                and validated[0]["message_id"] >= existing[-1]["message_id"]
            ):
                raise ValueError("History checkpoint repeated an older page")
            seen = {message["message_id"] for message in existing}
            for message in validated:
                if message["message_id"] not in seen:
                    existing.append(message)
                    seen.add(message["message_id"])
            pages = current_job.pages_completed + 1
            message_count_limited = len(existing) > MAX_JOB_MESSAGES
            budget_reached = (
                pages >= MAX_JOB_PAGES or len(existing) >= MAX_JOB_MESSAGES
            )
            if message_count_limited:
                existing = existing[:MAX_JOB_MESSAGES]
            messages_json = _bounded_json(
                existing, maximum=MAX_JOB_MESSAGES_BYTES, allow_oversize=True,
            )
            storage_limited = False
            while len(messages_json.encode("utf-8")) > MAX_JOB_MESSAGES_BYTES:
                existing.pop()
                storage_limited = True
                messages_json = _bounded_json(
                    existing, maximum=MAX_JOB_MESSAGES_BYTES, allow_oversize=True,
                )
            if storage_limited:
                validated_coverage["retained_storage_limit_reached"] = True
            if message_count_limited:
                validated_coverage["retained_message_limit_reached"] = True
            repeated_cursor = (
                next_cursor is not None
                and next_cursor == current_job.cursor
            )
            if repeated_cursor:
                validated_coverage["repeated_cursor_detected"] = True
            coverage_json = _bounded_json(
                validated_coverage, maximum=MAX_JOB_COVERAGE_BYTES,
            )
            if (
                storage_limited
                or message_count_limited
                or provider_limited
                or repeated_cursor
                or (
                    budget_reached and (next_cursor is not None or deep)
                )
            ):
                status, continuable, cursor = "partial", False, None
            elif deep and next_cursor is None:
                status, continuable, cursor = "waiting", True, current_job.cursor
            elif next_cursor is not None:
                status, continuable, cursor = "partial", True, next_cursor
            elif reached_end:
                status, continuable, cursor = "completed", False, None
            else:
                status, continuable, cursor = "partial", False, None
            updated = connection.execute(
                """UPDATE research_jobs SET cursor=?, status=?, messages_json=?,
                   pages_completed=?, total_results_estimate=?, coverage_json=?,
                   continuable=?, error_class=NULL, updated_at=?, expires_at=?,
                   version=version+1, lease_owner=NULL, lease_expires_at=NULL
                   WHERE job_id=? AND version=? AND lease_owner=?""",
                (
                    cursor, status,
                    messages_json,
                    pages, total,
                    coverage_json,
                    int(continuable), current, current + JOB_IDLE_SECONDS,
                    claimed.job_id, claimed.version, lease_owner,
                ),
            )
            if updated.rowcount != 1:
                raise ResearchJobConflict("Research job checkpoint conflicted")
            row = connection.execute(
                "SELECT * FROM research_jobs WHERE job_id=?", (claimed.job_id,),
            ).fetchone()
            return _decode_job(row)

    def release_retryable(
        self,
        claimed: DiscordResearchJob,
        *,
        lease_owner: str,
        error_class: str,
        now: float | None = None,
    ) -> None:
        current = _finite_time(time.time() if now is None else now)
        if not isinstance(error_class, str) or not error_class:
            raise ValueError("Invalid research job error class")
        status = "partial" if claimed.pages_completed else "queued"
        with self.connect() as connection, sqlite_transaction(
            connection, immediate=True,
        ):
            updated = connection.execute(
                """UPDATE research_jobs SET status=?, error_class=?,
                   lease_owner=NULL, lease_expires_at=NULL, updated_at=?,
                   expires_at=?, version=version+1
                   WHERE job_id=? AND version=? AND lease_owner=?""",
                (
                    status, error_class[:80], current,
                    current + JOB_IDLE_SECONDS, claimed.job_id,
                    claimed.version, lease_owner,
                ),
            )
        if updated.rowcount != 1:
            raise ResearchJobConflict("Research job release conflicted")

    def cancel(
        self,
        job_id: str,
        *,
        guild_id: int,
        conversation_root_id: int,
        requester_id: int,
        now: float | None = None,
    ) -> DiscordResearchJob | None:
        if not isinstance(job_id, str) or len(job_id) != 32:
            return None
        jobs = self.cancel_many(
            (job_id,), guild_id=guild_id,
            conversation_root_id=conversation_root_id,
            requester_id=requester_id, now=now,
        )
        return jobs[0] if jobs is not None else None

    def cancel_many(
        self,
        job_ids: Sequence[str],
        *,
        guild_id: int,
        conversation_root_id: int,
        requester_id: int,
        now: float | None = None,
    ) -> tuple[DiscordResearchJob, ...] | None:
        """Atomically cancel an exact requester-owned set without reading sources."""
        for value in (guild_id, conversation_root_id, requester_id):
            _positive_int(value)
        if (
            not isinstance(job_ids, Sequence)
            or not 1 <= len(job_ids) <= MAX_RESEARCH_BATCH_JOBS
            or any(not isinstance(value, str) or len(value) != 32 for value in job_ids)
            or len(set(job_ids)) != len(job_ids)
        ):
            raise ValueError("Invalid research job cancellation set")
        current = _finite_time(time.time() if now is None else now)
        placeholders = ",".join("?" for _ in job_ids)
        with self.connect() as connection, sqlite_transaction(
            connection, immediate=True,
        ):
            rows = connection.execute(
                f"""SELECT job_id, requester_id FROM research_jobs
                    WHERE job_id IN ({placeholders}) AND guild_id=?
                      AND conversation_root_id=? AND expires_at>?""",
                (*job_ids, guild_id, conversation_root_id, current),
            ).fetchall()
            if len(rows) != len(job_ids):
                return None
            if any(row["requester_id"] != requester_id for row in rows):
                raise PermissionError("Only the job requester can cancel it")
            connection.execute(
                f"""UPDATE research_jobs SET status='cancelled', continuable=0,
                    cancellation_requested=1, lease_owner=NULL,
                    lease_expires_at=NULL, updated_at=?, expires_at=?,
                    version=version+1 WHERE job_id IN ({placeholders})""",
                (current, current + JOB_IDLE_SECONDS, *job_ids),
            )
            rows = connection.execute(
                f"SELECT * FROM research_jobs WHERE job_id IN ({placeholders})",
                tuple(job_ids),
            ).fetchall()
        jobs = {}
        for row in rows:
            job = _decode_job(row)
            jobs[job.job_id] = job
        return tuple(jobs[job_id] for job_id in job_ids)

    def prune(self, *, now: float | None = None) -> int:
        current = _finite_time(time.time() if now is None else now)
        with self.connect() as connection, sqlite_transaction(
            connection, immediate=True,
        ):
            cursor = connection.execute(
                "DELETE FROM research_jobs WHERE expires_at <= ?", (current,),
            )
            return cursor.rowcount


__all__ = [
    "DiscordResearchJob", "ResearchJobBusy", "ResearchJobConflict",
    "ResearchJobRepository", "ResearchJobScope",
]
