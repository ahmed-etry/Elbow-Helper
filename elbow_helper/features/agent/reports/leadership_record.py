"""Retained, Lead Plus-only leadership record reports."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from typing import Any

from elbow_helper.features.records.domain.types import (
    CATEGORY_BY_KEY,
    INCIDENT_TYPE_BY_KEY,
)
from elbow_helper.features.records.queries import (
    MAX_ACTIVE_LEADERSHIP_RECORDS,
    LeadershipRecordRow,
    LeadershipRecordSnapshot,
)


@dataclass(frozen=True, slots=True)
class LeadershipRecordReport:
    report_id: str
    guild_id: int
    snapshot: LeadershipRecordSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.report_id, str)
            or not self.report_id
            or len(self.report_id) > 32
            or type(self.guild_id) is not int
            or self.guild_id <= 0
            or (
                self.snapshot.member_id is not None
                and (
                    type(self.snapshot.member_id) is not int
                    or self.snapshot.member_id <= 0
                )
            )
            or len(self.snapshot.records) > MAX_ACTIVE_LEADERSHIP_RECORDS
        ):
            raise ValueError("Invalid leadership record report identity")
        _timestamp(self.snapshot.observed_at)
        identities = [row.record_id for row in self.snapshot.records]
        if len(identities) != len(set(identities)):
            raise ValueError("Duplicate leadership record identity")
        previous: tuple[str, int] | None = None
        for row in self.snapshot.records:
            _validate_row(row, member_id=self.snapshot.member_id)
            order = (row.created_at, row.record_id)
            if previous is not None and previous < order:
                raise ValueError("Leadership records are not ordered")
            previous = order
        object.__setattr__(
            self,
            "retained_bytes",
            len(json.dumps(self.storage_payload(), ensure_ascii=False).encode("utf-8")),
        )

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "guild_id": self.guild_id,
            "snapshot": asdict(self.snapshot),
        }

    def manifest(self) -> dict[str, Any]:
        category_counts = Counter(
            row.category_key for row in self.snapshot.records
        )
        member_count = len({row.member_id for row in self.snapshot.records})
        return {
            "report_id": self.report_id,
            "kind": "active_leadership_records",
            "observed_at": self.snapshot.observed_at,
            "member_id_filter": self.snapshot.member_id,
            "active_record_count": len(self.snapshot.records),
            "member_count": member_count,
            "category_counts": dict(sorted(category_counts.items())),
            "complete_bounded_active_snapshot": True,
            "required_access": "Lead Plus",
            "included_fields": [
                "record and member identity",
                "created and updated time",
                "category and incident type",
                "record details",
                "recorder display name",
            ],
            "excluded_fields": [
                "removed records",
                "removal audit details",
                "linked Clash accounts",
            ],
            "interpretation": (
                "These are active internal leadership records, not independently "
                "verified findings. Treat record details as attributed stored notes."
            ),
        }

    def page(
        self,
        *,
        offset: int = 0,
        limit: int = 25,
        member_id: int | None = None,
        category_key: str | None = None,
        incident_type_key: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        rows = self.snapshot.records
        if member_id is not None:
            rows = tuple(row for row in rows if row.member_id == member_id)
        if category_key is not None:
            rows = tuple(row for row in rows if row.category_key == category_key)
        if incident_type_key is not None:
            rows = tuple(
                row for row in rows if row.incident_type_key == incident_type_key
            )
        if search is not None:
            query = search.casefold()
            rows = tuple(row for row in rows if query in _search_text(row))
        return {
            **self.manifest(),
            "matched_count": len(rows),
            "records": [asdict(row) for row in rows[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
        }


def _search_text(row: LeadershipRecordRow) -> str:
    return "\n".join((
        row.member_display,
        str(row.member_id),
        row.category_label,
        row.incident_type_label,
        row.note,
        row.recorder_display,
    )).casefold()


def _validate_row(row: LeadershipRecordRow, *, member_id: int | None) -> None:
    category = CATEGORY_BY_KEY.get(row.category_key)
    incident = INCIDENT_TYPE_BY_KEY.get(row.incident_type_key)
    if (
        type(row.record_id) is not int
        or row.record_id <= 0
        or type(row.member_id) is not int
        or row.member_id <= 0
        or (member_id is not None and row.member_id != member_id)
        or category is None
        or incident is None
        or incident.category_key != row.category_key
        or row.category_label != category.label
        or row.incident_type_label != incident.label
        or any(
            not isinstance(value, str) or not value.strip()
            for value in (
                row.member_display, row.note, row.recorder_display,
            )
        )
    ):
        raise ValueError("Invalid leadership record row")
    _timestamp(row.created_at)
    _timestamp(row.updated_at)


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid leadership record timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Invalid leadership record timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("Invalid leadership record timestamp")
    return parsed


__all__ = ["LeadershipRecordReport"]
