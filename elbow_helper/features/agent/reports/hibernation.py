"""Retained status-only active hibernation snapshots."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from typing import Any

from elbow_helper.configuration.channels import HIBERNATION_LOG
from elbow_helper.features.hibernation.queries import (
    ActiveHibernationRecord,
    ActiveHibernationSnapshot,
)


@dataclass(frozen=True, slots=True)
class HibernationReport:
    report_id: str
    guild_id: int
    source_channel_id: int
    snapshot: ActiveHibernationSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.report_id, str) or not self.report_id
            or len(self.report_id) > 32 or type(self.guild_id) is not int
            or self.guild_id <= 0 or self.source_channel_id != HIBERNATION_LOG
        ):
            raise ValueError("Invalid hibernation report identity")
        _timestamp(self.snapshot.observed_at)
        if (
            any(type(value) is not int or value < 0 for value in (
                self.snapshot.stored_member_entry_count,
                self.snapshot.skipped_invalid_member_entries,
                self.snapshot.ignored_metadata_entries,
                self.snapshot.missing_start_time_count,
            ))
            or self.snapshot.stored_member_entry_count != (
                len(self.snapshot.records)
                + self.snapshot.skipped_invalid_member_entries
            )
            or self.snapshot.missing_start_time_count != sum(
                row.start_time_status == "unavailable"
                for row in self.snapshot.records
            )
        ):
            raise ValueError("Invalid hibernation report coverage")
        member_ids = [row.member_id for row in self.snapshot.records]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("Duplicate member in hibernation report")
        previous = None
        for row in self.snapshot.records:
            _validate_record(row)
            order = (
                row.started_ts is None,
                -(row.started_ts or 0), row.member_id,
            )
            if previous is not None and previous > order:
                raise ValueError("Hibernation records are not ordered")
            previous = order
        object.__setattr__(self, "retained_bytes", len(json.dumps(
            self.storage_payload(), ensure_ascii=False,
        ).encode("utf-8")))

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "guild_id": self.guild_id,
            "source_channel_id": self.source_channel_id,
            "snapshot": asdict(self.snapshot),
        }

    def manifest(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "kind": "active_hibernation",
            "observed_at": self.snapshot.observed_at,
            "source_channel_id": self.source_channel_id,
            "active_record_count": len(self.snapshot.records),
            "missing_start_time_count": self.snapshot.missing_start_time_count,
            "skipped_invalid_member_entries": (
                self.snapshot.skipped_invalid_member_entries
            ),
            "ignored_metadata_entries": self.snapshot.ignored_metadata_entries,
            "complete_valid_record_snapshot": True,
            "included_fields": [
                "member_id", "started_at", "started_ts", "start_time_status",
            ],
            "excluded_sensitive_fields": [
                "saved_roles", "rank_roles", "ticket_metadata",
                "private_thread_content", "notices", "reasons",
            ],
            "interpretation": (
                "Each row is an active stored hibernation workflow record observed at "
                "observed_at. It can explain expected inactivity, but it does not expose "
                "a reason, verify current Discord roles, or prove that a private ticket "
                "is still open."
            ),
        }

    def page(
        self, *, offset: int = 0, limit: int = 25,
        member_id: int | None = None,
    ) -> dict[str, Any]:
        rows = self.snapshot.records
        if member_id is not None:
            rows = tuple(row for row in rows if row.member_id == member_id)
        return {
            **self.manifest(), "matched_count": len(rows),
            "records": [asdict(row) for row in rows[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
        }


def _validate_record(row: ActiveHibernationRecord) -> None:
    if type(row.member_id) is not int or row.member_id <= 0:
        raise ValueError("Invalid active hibernation member")
    if row.start_time_status == "recorded":
        if (
            type(row.started_ts) is not int or row.started_ts <= 0
            or not isinstance(row.started_at, str)
            or int(_timestamp(row.started_at).timestamp()) != row.started_ts
        ):
            raise ValueError("Invalid active hibernation start time")
    elif row.start_time_status == "unavailable":
        if row.started_at is not None or row.started_ts is not None:
            raise ValueError("Invalid unavailable hibernation start time")
    else:
        raise ValueError("Invalid hibernation start-time status")


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid hibernation timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Invalid hibernation timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("Invalid hibernation timestamp")
    return parsed


__all__ = ["HibernationReport"]
