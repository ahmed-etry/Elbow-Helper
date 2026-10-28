"""Restart-persistent event schedule evidence."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import re
from typing import Any

from elbow_helper.features.event_stats.queries import EventScheduleSnapshot


@dataclass(frozen=True, slots=True)
class EventScheduleReport:
    report_id: str
    guild_id: int
    snapshot: EventScheduleSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        rows = self.snapshot.rows
        if (
            not isinstance(self.report_id, str) or not self.report_id
            or len(self.report_id) > 32
            or type(self.guild_id) is not int or self.guild_id <= 0
            or not _valid_time(self.snapshot.observed_at)
            or len(rows) > 100
            or len({row.event_key for row in rows}) != len(rows)
            or len({row.position for row in rows}) != len(rows)
            or rows != tuple(sorted(
                rows, key=lambda row: (row.position, row.event_key),
            ))
            or any(not _valid_row(row) for row in rows)
        ):
            raise ValueError("Invalid event schedule report")
        object.__setattr__(self, "retained_bytes", len(json.dumps(
            self.storage_payload(), ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")))

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "guild_id": self.guild_id,
            "snapshot": asdict(self.snapshot),
        }

    def manifest(self) -> dict[str, Any]:
        rows = self.snapshot.rows
        return {
            "report_id": self.report_id, "kind": "event_schedule",
            "observed_at": self.snapshot.observed_at,
            "event_count": len(rows),
            "enabled_count": sum(row.enabled for row in rows),
            "phase_counts": dict(sorted(Counter(
                row.phase for row in rows
            ).items())),
            "counter_coverage_counts": dict(sorted(Counter(
                row.count_coverage for row in rows
                if row.count_coverage is not None
            ).items())),
        }

    def page(
        self, *, phase: str = "all", event_type: str = "all",
        offset: int = 0, limit: int = 25,
    ) -> dict[str, Any]:
        if (
            phase not in {
                "all", "live", "upcoming", "ended", "expired", "disabled",
            }
            or event_type not in {"all", "counter", "recurring", "one-time"}
            or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 25
        ):
            raise ValueError("Invalid event schedule page")
        rows = tuple(row for row in self.snapshot.rows if (
            (phase == "all" or row.phase == phase)
            and (event_type == "all" or row.event_type == event_type)
        ))
        return {
            **self.manifest(),
            "filters": {"phase": phase, "event_type": event_type},
            "matching_rows": len(rows),
            "events": [asdict(row) for row in rows[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "complete_snapshot": True,
        }


def _valid_row(row: Any) -> bool:
    scheduled = (row.start_at, row.end_at, row.next_at)
    counter_valid = (
        row.event_type == "counter"
        and row.schedule_shape == "counter"
        and all(value is None for value in scheduled)
        and type(row.member_count) is int and row.member_count >= 0
        and type(row.configured_role_count) is int
        and row.configured_role_count > 0
        and type(row.missing_role_count) is int
        and 0 <= row.missing_role_count <= row.configured_role_count
        and row.count_coverage in {"complete", "partial_missing_roles"}
        and (row.missing_role_count == 0) == (row.count_coverage == "complete")
    )
    range_valid = (
        row.event_type in {"recurring", "one-time"}
        and row.schedule_shape in {"range", "one-time"}
        and _valid_time(row.start_at) and _valid_time(row.end_at)
        and row.next_at is None and row.member_count is None
        and row.configured_role_count == row.missing_role_count == 0
        and row.count_coverage is None
        and _parse_time(row.start_at) < _parse_time(row.end_at)
    )
    point_valid = (
        row.event_type == "recurring" and row.schedule_shape == "point"
        and row.start_at is None and row.end_at is None
        and _valid_time(row.next_at) and row.member_count is None
        and row.configured_role_count == row.missing_role_count == 0
        and row.count_coverage is None
    )
    return bool(
        isinstance(row.event_key, str)
        and re.fullmatch(r"[a-z0-9_]{1,100}", row.event_key)
        and isinstance(row.name, str) and row.name and len(row.name) <= 100
        and row.source in {"preset", "custom"}
        and row.event_type in {"counter", "recurring", "one-time"}
        and type(row.enabled) is bool
        and row.phase in {"live", "upcoming", "ended", "expired", "disabled"}
        and row.enabled == (row.phase != "disabled")
        and type(row.position) is int and row.position >= 0
        and (row.timezone is None or isinstance(row.timezone, str) and row.timezone)
        and (row.event_type == "one-time") == (row.timezone is not None)
        and type(row.grace_period_hours) is int
        and 0 <= row.grace_period_hours <= 168
        and (counter_valid or range_valid or point_valid)
    )


def _valid_time(value: Any) -> bool:
    return _parse_time(value) is not None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


__all__ = ["EventScheduleReport"]
