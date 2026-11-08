"""Restart-persistent member-lifecycle evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from typing import Any

from elbow_helper.features.member_lifecycle.queries import MemberLifecycleSnapshot


@dataclass(frozen=True, slots=True)
class MemberLifecycleReport:
    report_id: str
    guild_id: int
    source_channel_id: int
    snapshot: MemberLifecycleSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        rows = self.snapshot.rows
        overdue = self.snapshot.overdue_applicants
        if (
            not isinstance(self.report_id, str) or not self.report_id
            or len(self.report_id) > 32
            or type(self.guild_id) is not int or self.guild_id <= 0
            or type(self.source_channel_id) is not int
            or self.source_channel_id <= 0
            or not _valid_time(self.snapshot.observed_at)
            or not _valid_optional_time(self.snapshot.last_weekly_report_at)
            or not _valid_optional_time(self.snapshot.last_applicant_scan_at)
            or any(
                type(value) is not int or value < 0
                for value in (
                    self.snapshot.stored_member_entry_count,
                    self.snapshot.current_guild_member_count,
                    self.snapshot.skipped_invalid_member_entries,
                    self.snapshot.untracked_current_member_count,
                    self.snapshot.skipped_invalid_platform_counts,
                    self.snapshot.skipped_invalid_overdue_entries,
                )
            )
            or len(rows) > 10_000 or len(overdue) > 10_000
            or len({row.member_id for row in rows}) != len(rows)
            or len({row.member_id for row in overdue}) != len(overdue)
            or rows != tuple(sorted(
                rows, key=lambda item: (item.joined_at, item.member_id),
                reverse=True,
            ))
            or any(not _valid_row(row) for row in rows)
            or any(
                type(item.member_id) is not int or item.member_id <= 0
                or not isinstance(item.display_name, str)
                or not item.display_name or len(item.display_name) > 100
                for item in overdue
            )
            or not _valid_platform_counts(self.snapshot.platform_counts)
            or len(rows) + self.snapshot.untracked_current_member_count
                != self.snapshot.current_guild_member_count
        ):
            raise ValueError("Invalid member lifecycle report")
        overdue_ids = {item.member_id for item in overdue}
        if any(row.overdue_applicant != (row.member_id in overdue_ids) for row in rows):
            raise ValueError("Invalid member lifecycle report")
        object.__setattr__(self, "retained_bytes", len(json.dumps(
            self.storage_payload(), ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")))

    @property
    def source_channels(self) -> frozenset[int]:
        return frozenset({
            self.source_channel_id,
            *(row.last_seen_channel_id for row in self.snapshot.rows
              if row.last_seen_channel_id is not None),
        })

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "guild_id": self.guild_id,
            "source_channel_id": self.source_channel_id,
            "snapshot": asdict(self.snapshot),
        }

    def manifest(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "kind": "member_lifecycle",
            "observed_at": self.snapshot.observed_at,
            "tracked_current_member_count": len(self.snapshot.rows),
            "current_guild_member_count": self.snapshot.current_guild_member_count,
            "untracked_current_member_count": self.snapshot.untracked_current_member_count,
            "overdue_applicant_count": len(self.snapshot.overdue_applicants),
            "activity_observation_count": sum(
                row.last_seen_at is not None for row in self.snapshot.rows
            ),
            "coverage": {
                "stored_member_entry_count": self.snapshot.stored_member_entry_count,
                "skipped_invalid_member_entries": self.snapshot.skipped_invalid_member_entries,
                "skipped_invalid_platform_counts": self.snapshot.skipped_invalid_platform_counts,
                "skipped_invalid_overdue_entries": self.snapshot.skipped_invalid_overdue_entries,
            },
            "last_weekly_report_at": self.snapshot.last_weekly_report_at,
            "last_applicant_scan_at": self.snapshot.last_applicant_scan_at,
            "platform_counts": [
                {"platform": platform, "count": count}
                for platform, count in self.snapshot.platform_counts
            ],
            "overdue_applicants": [
                asdict(item) for item in self.snapshot.overdue_applicants
            ],
        }

    def page(
        self,
        *,
        platform: str | None = None,
        activity: str = "all",
        overdue_only: bool = False,
        offset: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        if (
            platform is not None and (
                not isinstance(platform, str) or not platform
                or len(platform) > 100
            )
            or activity not in {"all", "observed", "not_observed"}
            or type(overdue_only) is not bool
            or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 25
        ):
            raise ValueError("Invalid member lifecycle page")
        rows = tuple(row for row in self.snapshot.rows if (
            (platform is None or row.platform.casefold() == platform.casefold())
            and (not overdue_only or row.overdue_applicant)
            and (
                activity == "all"
                or (row.last_seen_at is not None) == (activity == "observed")
            )
        ))
        return {
            **self.manifest(),
            "filters": {
                "platform": platform,
                "activity": activity,
                "overdue_only": overdue_only,
            },
            "matching_rows": len(rows),
            "members": [asdict(row) for row in rows[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "complete_snapshot": True,
        }


def _valid_row(row: Any) -> bool:
    return bool(
        type(row.member_id) is int and row.member_id > 0
        and isinstance(row.display_name, str) and row.display_name
        and len(row.display_name) <= 100
        and _valid_time(row.joined_at)
        and isinstance(row.platform, str) and row.platform
        and len(row.platform) <= 100
        and type(row.overdue_applicant) is bool
        and (
            row.last_seen_at is None and row.last_seen_channel_id is None
            or _valid_time(row.last_seen_at)
            and type(row.last_seen_channel_id) is int
            and row.last_seen_channel_id > 0
        )
    )


def _valid_platform_counts(values: Any) -> bool:
    return bool(
        isinstance(values, tuple) and len(values) <= 100
        and all(
            isinstance(item, tuple) and len(item) == 2
            and isinstance(item[0], str) and item[0] and len(item[0]) <= 100
            and type(item[1]) is int and item[1] >= 0
            for item in values
        )
        and len({item[0] for item in values}) == len(values)
    )


def _valid_optional_time(value: Any) -> bool:
    return value is None or _valid_time(value)


def _valid_time(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


__all__ = ["MemberLifecycleReport"]
