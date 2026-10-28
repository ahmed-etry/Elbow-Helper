"""Restart-persistent achievement evidence reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
import re
from typing import Any

from elbow_helper.features.achievements.queries import MemberAchievementSnapshot


@dataclass(frozen=True, slots=True)
class AchievementProgressReport:
    report_id: str
    guild_id: int
    member_name: str
    snapshot: MemberAchievementSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        value = self.snapshot
        if (
            not _valid_identity(self.report_id, self.guild_id)
            or not isinstance(self.member_name, str) or not self.member_name
            or len(self.member_name) > 100
            or not isinstance(value.observed_at, str) or not value.observed_at
            or type(value.member_id) is not int or value.member_id <= 0
            or type(value.completed_count) is not int
            or value.completed_count != sum(row.completed for row in value.rows)
            or type(value.total_count) is not int
            or value.total_count != len(value.rows)
            or value.total_count > 100
            or len({row.achievement_id for row in value.rows}) != len(value.rows)
            or any(not _valid_progress_row(row) for row in value.rows)
        ):
            raise ValueError("Invalid achievement progress report")
        object.__setattr__(
            self, "retained_bytes", _payload_bytes(self.storage_payload()),
        )

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "guild_id": self.guild_id,
            "member_name": self.member_name,
            "snapshot": asdict(self.snapshot),
        }

    def manifest(self) -> dict[str, Any]:
        value = self.snapshot
        return {
            "report_id": self.report_id, "kind": "achievement_progress",
            "observed_at": value.observed_at,
            "member_id": value.member_id, "member_name": self.member_name,
            "completed_count": value.completed_count,
            "in_progress_count": value.total_count - value.completed_count,
            "total_count": value.total_count,
        }

    def page(
        self, *, status: str = "all", offset: int = 0, limit: int = 25,
    ) -> dict[str, Any]:
        if (
            status not in {"all", "completed", "in_progress"}
            or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 25
        ):
            raise ValueError("Invalid achievement progress page")
        rows = tuple(row for row in self.snapshot.rows if (
            status == "all"
            or status == "completed" and row.completed
            or status == "in_progress" and not row.completed
        ))
        return {
            **self.manifest(), "status_filter": status,
            "matching_rows": len(rows),
            "achievements": [
                asdict(row) for row in rows[offset:offset + limit]
            ],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "complete_snapshot": True,
        }


def _valid_identity(report_id: Any, guild_id: Any) -> bool:
    return bool(
        isinstance(report_id, str) and report_id and len(report_id) <= 32
        and type(guild_id) is int and guild_id > 0
    )


def _valid_progress_row(row: Any) -> bool:
    return bool(
        isinstance(row.achievement_id, str)
        and re.fullmatch(r"[a-z0-9_]{1,64}", row.achievement_id)
        and isinstance(row.name, str) and row.name and len(row.name) <= 100
        and isinstance(row.description, str) and len(row.description) <= 500
        and type(row.required_count) is int and row.required_count > 0
        and row.progress_kind in {
            "counter", "membership_days", "completion_only",
        }
        and isinstance(row.current_count, (int, float))
        and not isinstance(row.current_count, bool)
        and math.isfinite(float(row.current_count))
        and row.current_count >= 0
        and type(row.completed) is bool
        and (
            row.completed_at is None
            or type(row.completed_at) is int and row.completed_at >= 0
        )
        and row.completed == (row.completed_at is not None)
    )


def _payload_bytes(value: dict[str, Any]) -> int:
    return len(json.dumps(
        value, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8"))
__all__ = ["AchievementProgressReport"]
