"""Typed contracts and validation for durable Discord research jobs."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import sqlite3
from typing import Any


JOB_IDLE_SECONDS = 6 * 60 * 60
JOB_LEASE_SECONDS = 60
MAX_JOB_PAGES = 20
MAX_JOB_MESSAGES = 250
MAX_JOB_PAGE_SIZE = 25
MAX_SEARCH_JOB_PAGE_SIZE = 10
MAX_JOB_MESSAGES_BYTES = 512 * 1024
MAX_JOB_COVERAGE_BYTES = 8 * 1024
MAX_RESEARCH_BATCH_JOBS = 3
JOB_STATUSES = frozenset({
    "queued", "running", "waiting", "completed", "partial", "failed",
    "cancelled",
})
JOB_KINDS = frozenset({"search", "history"})


@dataclass(frozen=True, slots=True)
class ResearchJobDefinition:
    source_channel_id: int
    query: str
    author_id: int | None
    after: str | None
    before: str | None
    page_size: int
    kind: str = "search"

    def __post_init__(self) -> None:
        if (
            positive_int(self.source_channel_id) != self.source_channel_id
            or self.author_id is not None
            and positive_int(self.author_id) != self.author_id
            or not isinstance(self.query, str)
            or len(self.query) > 1024
            or self.kind not in JOB_KINDS
            or (
                self.kind == "history"
                and (self.query or self.after is None or self.before is None)
            )
            or type(self.page_size) is not int
            or not 1 <= self.page_size <= (
                MAX_SEARCH_JOB_PAGE_SIZE
                if self.kind == "search" else MAX_JOB_PAGE_SIZE
            )
            or any(
                value is not None
                and (not isinstance(value, str) or len(value) > 40)
                for value in (self.after, self.before)
            )
        ):
            raise ValueError("Invalid research job definition")


@dataclass(frozen=True, slots=True)
class ResearchJobScope:
    job_id: str
    guild_id: int
    conversation_root_id: int
    requester_id: int
    source_channel_id: int
    status: str


@dataclass(frozen=True, slots=True)
class DiscordResearchJob:
    job_id: str
    guild_id: int
    conversation_root_id: int
    requester_id: int
    source_channel_id: int
    kind: str
    query: str
    author_id: int | None
    after: str | None
    before: str | None
    page_size: int
    cursor: str | None
    status: str
    messages: tuple[dict[str, Any], ...]
    pages_completed: int
    total_results_estimate: int | None
    coverage: dict[str, Any] | None
    continuable: bool
    cancellation_requested: bool
    error_class: str | None
    created_at: float
    updated_at: float
    expires_at: float
    version: int
    lease_owner: str | None
    lease_expires_at: float | None

    def manifest(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "kind": self.kind,
            "source_channel_id": self.source_channel_id,
            "query": self.query,
            "author_id": self.author_id,
            "after": self.after,
            "before": self.before,
            "page_size": self.page_size,
            "pages_completed": self.pages_completed,
            "retained_message_count": len(self.messages),
            "total_results_estimate": self.total_results_estimate,
            "continuable": self.continuable,
            "cancellation_requested": self.cancellation_requested,
            "error_class": self.error_class,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "version": self.version,
            "coverage": self.coverage,
        }


def decode_research_job(row: sqlite3.Row) -> DiscordResearchJob:
    if (
        not isinstance(row["messages_json"], str)
        or len(row["messages_json"].encode("utf-8")) > MAX_JOB_MESSAGES_BYTES
        or row["coverage_json"] is not None
        and (
            not isinstance(row["coverage_json"], str)
            or len(row["coverage_json"].encode("utf-8")) > MAX_JOB_COVERAGE_BYTES
        )
    ):
        raise ValueError("Persisted research job exceeds its storage bound")
    messages = json.loads(row["messages_json"])
    coverage = (
        json.loads(row["coverage_json"])
        if row["coverage_json"] is not None else None
    )
    if not isinstance(messages, list) or (
        coverage is not None and not isinstance(coverage, dict)
    ):
        raise ValueError("Invalid persisted research job")
    kind = row["kind"]
    decoded_messages = tuple(validate_research_message(value) for value in messages)
    decoded_coverage = (
        validate_research_coverage(coverage, kind=kind)
        if coverage is not None else None
    )
    status = row["status"]
    source_channel_id = positive_int(row["source_channel_id"])
    author_id = row["author_id"]
    total = row["total_results_estimate"]
    lease_owner = row["lease_owner"]
    lease_expires_at = row["lease_expires_at"]
    if (
        status not in JOB_STATUSES
        or kind not in JOB_KINDS
        or not isinstance(row["job_id"], str)
        or len(row["job_id"]) != 32
        or not isinstance(row["query"], str)
        or len(row["query"]) > 1024
        or (
            kind == "history"
            and (
                bool(row["query"])
                or row["after_value"] is None
                or row["before_value"] is None
                or total is not None
            )
        )
        or author_id is not None and positive_int(author_id) != author_id
        or any(
            value is not None
            and (not isinstance(value, str) or len(value) > 40)
            for value in (row["after_value"], row["before_value"])
        )
        or type(row["page_size"]) is not int
        or not 1 <= row["page_size"] <= (
            MAX_SEARCH_JOB_PAGE_SIZE
            if kind == "search" else MAX_JOB_PAGE_SIZE
        )
        or row["cursor"] is not None
        and (not isinstance(row["cursor"], str) or len(row["cursor"]) > 64)
        or type(row["pages_completed"]) is not int
        or not 0 <= row["pages_completed"] <= MAX_JOB_PAGES
        or len(decoded_messages) > MAX_JOB_MESSAGES
        or total is not None and (type(total) is not int or total < 0)
        or row["continuable"] not in (0, 1)
        or row["cancellation_requested"] not in (0, 1)
        or row["error_class"] is not None
        and (not isinstance(row["error_class"], str) or len(row["error_class"]) > 80)
        or type(row["version"]) is not int
        or row["version"] < 0
    ):
        raise ValueError("Invalid persisted research job status")
    created_at = finite_time(row["created_at"])
    updated_at = finite_time(row["updated_at"])
    expires_at = finite_time(row["expires_at"])
    lease_valid = (
        isinstance(lease_owner, str)
        and bool(lease_owner)
        and lease_expires_at is not None
    )
    if (
        created_at > updated_at
        or updated_at > expires_at
        or expires_at - updated_at > JOB_IDLE_SECONDS
        or any(message["channel_id"] != source_channel_id for message in decoded_messages)
        or author_id is not None
        and any(message["author_id"] != author_id for message in decoded_messages)
        or kind == "history"
        and any(
            current["message_id"] <= following["message_id"]
            for current, following in zip(decoded_messages, decoded_messages[1:])
        )
        or kind == "history"
        and decoded_coverage is not None
        and any(
            not decoded_coverage["window_after_message_id"]
            < message["message_id"]
            < decoded_coverage["snapshot_before_message_id"]
            for message in decoded_messages
        )
        or status == "running" and not lease_valid
        or status != "running"
        and (lease_owner is not None or lease_expires_at is not None)
        or lease_expires_at is not None and finite_time(lease_expires_at) <= 0
        or status in {"completed", "cancelled", "failed"}
        and bool(row["continuable"])
    ):
        raise ValueError("Invalid persisted research job state")
    return DiscordResearchJob(
        job_id=row["job_id"], guild_id=positive_int(row["guild_id"]),
        conversation_root_id=positive_int(row["conversation_root_id"]),
        requester_id=positive_int(row["requester_id"]),
        source_channel_id=source_channel_id,
        kind=kind, query=row["query"], author_id=author_id,
        after=row["after_value"], before=row["before_value"],
        page_size=row["page_size"], cursor=row["cursor"], status=status,
        messages=decoded_messages, pages_completed=row["pages_completed"],
        total_results_estimate=total, coverage=decoded_coverage,
        continuable=bool(row["continuable"]),
        cancellation_requested=bool(row["cancellation_requested"]),
        error_class=row["error_class"], created_at=created_at,
        updated_at=updated_at, expires_at=expires_at,
        version=row["version"], lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
    )


def validate_research_message(value: Any) -> dict[str, Any]:
    """Validate and copy one persisted Discord research result."""
    if not isinstance(value, dict):
        raise ValueError("Invalid persisted research message")
    required = {
        "message_id", "channel_id", "channel", "author_id", "author",
        "timestamp", "content", "source",
    }
    if set(value) != required:
        raise ValueError("Invalid persisted research message")
    for key in ("message_id", "channel_id", "author_id"):
        positive_int(value[key])
    if any(not isinstance(value[key], str) for key in required - {
        "message_id", "channel_id", "author_id",
    }):
        raise ValueError("Invalid persisted research message")
    if len(value["content"]) > 1_600:
        raise ValueError("Persisted research message exceeds its bound")
    for key, limit in (
        ("channel", 100), ("author", 100), ("timestamp", 64), ("source", 200),
    ):
        if not value[key] or len(value[key]) > limit:
            raise ValueError("Persisted research message exceeds its bound")
    return dict(value)


def validate_research_coverage(value: Any, *, kind: str) -> dict[str, Any]:
    """Validate and copy persisted coverage for a supported research kind."""
    if not isinstance(value, dict):
        raise ValueError("Invalid research job coverage")
    if kind == "search":
        return _validate_search_coverage(value)
    if kind == "history":
        return _validate_history_coverage(value)
    raise ValueError("Invalid research job kind")


def _validate_search_coverage(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "next_cursor", "total_results_estimate", "deep_historical_indexing",
        "offset_limit_reached", "reached_current_indexed_end",
    }
    optional = {
        "offset", "requested_page_size", "returned_indexed_matches",
        "returned_accessible_matches", "next_offset", "snapshot_before_message_id",
        "total_may_change_while_messages_are_created_or_deleted",
        "retained_storage_limit_reached", "retained_message_limit_reached",
        "repeated_cursor_detected",
    }
    if not required <= value.keys() or not value.keys() <= required | optional:
        raise ValueError("Invalid research job coverage")
    cursor = value["next_cursor"]
    total = value["total_results_estimate"]
    if (
        cursor is not None and (not isinstance(cursor, str) or len(cursor) > 64)
        or type(total) is not int or total < 0
        or any(type(value[key]) is not bool for key in (
            "deep_historical_indexing", "offset_limit_reached",
            "reached_current_indexed_end",
        ))
    ):
        raise ValueError("Invalid research job coverage")
    for key in (
        "offset", "requested_page_size", "returned_indexed_matches",
        "returned_accessible_matches", "next_offset", "snapshot_before_message_id",
    ):
        item = value.get(key)
        if item is not None and (type(item) is not int or item < 0):
            raise ValueError("Invalid research job coverage")
    for key in (
        "total_may_change_while_messages_are_created_or_deleted",
        "retained_storage_limit_reached", "retained_message_limit_reached",
        "repeated_cursor_detected",
    ):
        if key in value and type(value[key]) is not bool:
            raise ValueError("Invalid research job coverage")
    page_size = value.get("requested_page_size")
    indexed = value.get("returned_indexed_matches")
    accessible = value.get("returned_accessible_matches")
    if (
        page_size is not None and not 1 <= page_size <= MAX_SEARCH_JOB_PAGE_SIZE
        or indexed is not None and page_size is not None and indexed > page_size
        or accessible is not None and indexed is not None and accessible > indexed
        or value.get("next_offset") is not None and value["next_offset"] > 9_975
        or value.get("snapshot_before_message_id") is not None
        and value["snapshot_before_message_id"] <= 0
    ):
        raise ValueError("Invalid research job coverage")
    return dict(value)


def _validate_history_coverage(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "requested_after", "requested_before", "window_after_message_id",
        "snapshot_before_message_id", "page_before_message_id",
        "scanned_messages_in_window", "returned_messages", "next_cursor",
        "reached_requested_start", "covers_currently_available_messages_only",
    }
    optional = {
        "retained_storage_limit_reached", "retained_message_limit_reached",
        "repeated_cursor_detected",
    }
    if not required <= value.keys() or not value.keys() <= required | optional:
        raise ValueError("Invalid research job history coverage")
    cursor = value["next_cursor"]
    requested_after = value["requested_after"]
    requested_before = value["requested_before"]
    after_id = value["window_after_message_id"]
    snapshot_id = value["snapshot_before_message_id"]
    page_id = value["page_before_message_id"]
    scanned = value["scanned_messages_in_window"]
    returned = value["returned_messages"]
    reached_start = value["reached_requested_start"]
    if (
        not isinstance(requested_after, str) or not requested_after
        or len(requested_after) > 40
        or requested_before is not None and (
            not isinstance(requested_before, str) or not requested_before
            or len(requested_before) > 40
        )
        or any(type(item) is not int or item <= 0 for item in (
            after_id, snapshot_id, page_id,
        ))
        or not after_id < page_id <= snapshot_id
        or type(scanned) is not int or not 0 <= scanned <= MAX_JOB_PAGE_SIZE
        or type(returned) is not int or not 0 <= returned <= scanned
        or cursor is not None and (not isinstance(cursor, str) or len(cursor) > 64)
        or type(reached_start) is not bool or reached_start != (cursor is None)
        or value["covers_currently_available_messages_only"] is not True
    ):
        raise ValueError("Invalid research job history coverage")
    for key in optional:
        if key in value and type(value[key]) is not bool:
            raise ValueError("Invalid research job history coverage")
    return dict(value)


def bounded_json(value: Any, *, maximum: int, allow_oversize: bool = False) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if not allow_oversize and len(encoded.encode("utf-8")) > maximum:
        raise ValueError("Research job payload exceeds its storage bound")
    return encoded


def positive_int(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("Invalid research job identity")
    return value


def finite_time(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("Invalid research job time")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Invalid research job time")
    return value


__all__ = [
    "DiscordResearchJob", "JOB_IDLE_SECONDS", "JOB_KINDS", "JOB_LEASE_SECONDS",
    "JOB_STATUSES", "MAX_JOB_COVERAGE_BYTES", "MAX_JOB_MESSAGES",
    "MAX_JOB_MESSAGES_BYTES", "MAX_JOB_PAGES", "MAX_JOB_PAGE_SIZE",
    "MAX_RESEARCH_BATCH_JOBS", "MAX_SEARCH_JOB_PAGE_SIZE",
    "ResearchJobDefinition", "ResearchJobScope", "bounded_json",
    "decode_research_job", "finite_time", "positive_int",
    "validate_research_coverage", "validate_research_message",
]
