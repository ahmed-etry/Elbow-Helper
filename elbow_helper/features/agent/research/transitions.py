"""Checkpoint, cancellation, and retention transitions for research jobs."""

from __future__ import annotations
import time
from typing import Any, Mapping, Sequence
from elbow_helper.infrastructure.persistence import sqlite_transaction
from .contracts import (
    DiscordResearchJob,
    JOB_IDLE_SECONDS,
    MAX_JOB_COVERAGE_BYTES,
    MAX_JOB_MESSAGES,
    MAX_JOB_MESSAGES_BYTES,
    MAX_JOB_PAGES,
    MAX_RESEARCH_BATCH_JOBS,
    bounded_json as _bounded_json,
    decode_research_job as _decode_job,
    finite_time as _finite_time,
    positive_int as _positive_int,
    validate_research_coverage,
    validate_research_message,
)
from .contracts import ResearchJobConflict


class ResearchTransitionStore:
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
