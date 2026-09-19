"""Terminal Discord research jobs retained as bounded evidence artifacts."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any
from ..research.contracts import (
    DiscordResearchJob, MAX_JOB_MESSAGES,
    validate_research_coverage, validate_research_message,
)


TERMINAL_RESEARCH_REPORT_STATUSES = frozenset({"completed", "partial"})


@dataclass(frozen=True, slots=True)
class DiscordResearchReport:
    report_id: str
    guild_id: int
    source_job_id: str
    source_channel_id: int
    research_kind: str
    query: str
    author_id: int | None
    after: str | None
    before: str | None
    status: str
    pages_completed: int
    total_results_estimate: int | None
    coverage: dict[str, Any]
    messages: tuple[dict[str, Any], ...]
    finished_at: str
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            finished_at = datetime.fromisoformat(self.finished_at)
        except (TypeError, ValueError):
            finished_at = None
        if (
            not isinstance(self.report_id, str) or not self.report_id
            or type(self.guild_id) is not int or self.guild_id <= 0
            or not isinstance(self.source_job_id, str) or not self.source_job_id
            or type(self.source_channel_id) is not int or self.source_channel_id <= 0
            or self.research_kind not in {"search", "history"}
            or not isinstance(self.query, str) or len(self.query) > 1024
            or (
                self.author_id is not None
                and (type(self.author_id) is not int or self.author_id <= 0)
            )
            or any(
                value is not None
                and (not isinstance(value, str) or not value or len(value) > 40)
                for value in (self.after, self.before)
            )
            or self.status not in TERMINAL_RESEARCH_REPORT_STATUSES
            or type(self.pages_completed) is not int or self.pages_completed < 1
            or (
                self.total_results_estimate is not None
                and (
                    type(self.total_results_estimate) is not int
                    or self.total_results_estimate < 0
                )
            )
            or not 0 <= len(self.messages) <= MAX_JOB_MESSAGES
            or finished_at is None or finished_at.tzinfo is None
        ):
            raise ValueError("Invalid Discord research report")
        validated_messages = tuple(
            validate_research_message(message) for message in self.messages
        )
        validated_coverage = validate_research_coverage(
            self.coverage, kind=self.research_kind,
        )
        if (
            validated_messages != self.messages
            or validated_coverage != self.coverage
            or any(
                message["channel_id"] != self.source_channel_id
                or (
                    self.author_id is not None
                    and message["author_id"] != self.author_id
                )
                for message in self.messages
            )
            or (
                self.status == "completed"
                and not _coverage_complete(self.research_kind, self.coverage)
            )
            or (
                self.status == "partial"
                and _coverage_complete(self.research_kind, self.coverage)
            )
        ):
            raise ValueError("Discord research report does not match its scope")
        object.__setattr__(
            self, "retained_bytes",
            len(json.dumps(
                self.storage_payload(), ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")),
        )

    @classmethod
    def from_job(
        cls, report_id: str, job: DiscordResearchJob,
    ) -> DiscordResearchReport:
        if (
            job.status not in TERMINAL_RESEARCH_REPORT_STATUSES
            or job.continuable
            or job.coverage is None
            or job.pages_completed < 1
        ):
            raise ValueError("Research job is not a terminal evidence snapshot")
        return cls(
            report_id=report_id, guild_id=job.guild_id,
            source_job_id=job.job_id,
            source_channel_id=job.source_channel_id,
            research_kind=job.kind, query=job.query, author_id=job.author_id,
            after=job.after, before=job.before, status=job.status,
            pages_completed=job.pages_completed,
            total_results_estimate=job.total_results_estimate,
            coverage=dict(job.coverage),
            messages=tuple(dict(message) for message in job.messages),
            finished_at=datetime.fromtimestamp(
                job.updated_at, timezone.utc,
            ).isoformat(),
        )

    def manifest(self) -> dict[str, Any]:
        timestamps = sorted(message["timestamp"] for message in self.messages)
        author_counts = Counter(
            str(message["author_id"]) for message in self.messages
        )
        return {
            "report_id": self.report_id,
            "kind": "discord_research",
            "source_job_id": self.source_job_id,
            "research_topic_fingerprint": self.topic_fingerprint,
            "source_channel_id": self.source_channel_id,
            "research_kind": self.research_kind,
            "query": self.query,
            "author_id": self.author_id,
            "after": self.after,
            "before": self.before,
            "observed_at": self.finished_at,
            "job_status": self.status,
            "coverage_complete": self.status == "completed",
            "pages_completed": self.pages_completed,
            "retained_message_count": len(self.messages),
            "total_results_estimate": self.total_results_estimate,
            "message_ids_sha256": self.message_ids_sha256,
            "messages_sha256": self.messages_sha256,
            "oldest_message_at": timestamps[0] if timestamps else None,
            "newest_message_at": timestamps[-1] if timestamps else None,
            "author_counts": dict(sorted(author_counts.items())),
            "coverage": self.coverage,
            "limitations": _limitations(self.status, self.coverage),
        }

    @property
    def topic_fingerprint(self) -> str:
        return _sha256({
            "guild_id": self.guild_id,
            "source_channel_id": self.source_channel_id,
            "research_kind": self.research_kind,
            "query": self.query,
            "author_id": self.author_id,
        })

    @property
    def message_ids_sha256(self) -> str:
        return _sha256([message["message_id"] for message in self.messages])

    @property
    def messages_sha256(self) -> str:
        return _sha256(self.messages)

    def page(self, *, offset: int = 0, limit: int = 25) -> dict[str, Any]:
        if (
            type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 25
        ):
            raise ValueError("Invalid Discord research report page")
        return {
            **self.manifest(),
            "messages": list(self.messages[offset:offset + limit]),
            "next_offset": (
                offset + limit if offset + limit < len(self.messages) else None
            ),
            "complete_retained_snapshot": True,
            "interpretation": (
                "Messages are exact retained results from the stated channel and scope. "
                "Coverage describes what was searched or traversed. Missing results do "
                "not prove that no other message ever existed."
            ),
        }

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "guild_id": self.guild_id,
            "source_job_id": self.source_job_id,
            "source_channel_id": self.source_channel_id,
            "research_kind": self.research_kind, "query": self.query,
            "author_id": self.author_id, "after": self.after,
            "before": self.before, "status": self.status,
            "pages_completed": self.pages_completed,
            "total_results_estimate": self.total_results_estimate,
            "coverage": self.coverage, "messages": list(self.messages),
            "finished_at": self.finished_at,
        }


def _coverage_complete(kind: str, coverage: dict[str, Any]) -> bool:
    limited = any(coverage.get(key) is True for key in (
        "offset_limit_reached", "retained_storage_limit_reached",
        "retained_message_limit_reached", "repeated_cursor_detected",
        "deep_historical_indexing",
    ))
    if kind == "search":
        return bool(coverage["reached_current_indexed_end"]) and not limited
    return bool(coverage["reached_requested_start"]) and not limited


def _limitations(status: str, coverage: dict[str, Any]) -> list[str]:
    limitations = []
    if status == "partial":
        limitations.append("terminal_partial_coverage")
    for key in (
        "offset_limit_reached", "retained_storage_limit_reached",
        "retained_message_limit_reached", "repeated_cursor_detected",
    ):
        if coverage.get(key) is True:
            limitations.append(key)
    if coverage.get("deep_historical_indexing") is True:
        limitations.append("deep_historical_indexing")
    if coverage.get("covers_currently_available_messages_only") is True:
        limitations.append("currently_available_messages_only")
    return limitations


def _sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "DiscordResearchReport", "TERMINAL_RESEARCH_REPORT_STATUSES",
]
