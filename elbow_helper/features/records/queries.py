"""Bounded, read-only leadership-record projections for other features."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .domain.types import CATEGORY_BY_KEY, INCIDENT_TYPE_BY_KEY


MAX_ACTIVE_LEADERSHIP_RECORDS = 1_000


class LeadershipRecordReader(Protocol):
    def list(
        self,
        *,
        member_id: int | None = None,
        include_removed: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class LeadershipRecordRow:
    record_id: int
    created_at: str
    updated_at: str
    member_id: int
    member_display: str
    category_key: str
    category_label: str
    incident_type_key: str
    incident_type_label: str
    note: str
    recorder_display: str


@dataclass(frozen=True, slots=True)
class LeadershipRecordSnapshot:
    observed_at: str
    member_id: int | None
    records: tuple[LeadershipRecordRow, ...]


class LeadershipRecordQueries:
    """Project active records without exposing repository or mutation access."""

    def __init__(
        self,
        reader: LeadershipRecordReader,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._reader = reader
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def active_snapshot(
        self,
        *,
        member_id: int | None = None,
    ) -> LeadershipRecordSnapshot:
        if member_id is not None and (
            type(member_id) is not int or member_id <= 0
        ):
            raise ValueError("Invalid leadership record member identity")
        values = self._reader.list(
            member_id=member_id,
            include_removed=False,
            limit=MAX_ACTIVE_LEADERSHIP_RECORDS + 1,
        )
        if not isinstance(values, list):
            raise ValueError("Invalid leadership record result")
        if len(values) > MAX_ACTIVE_LEADERSHIP_RECORDS:
            raise ValueError("Leadership record result exceeds its read bound")
        observed = self._clock()
        if not isinstance(observed, datetime):
            raise ValueError("Invalid leadership record observation time")
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        observed = observed.astimezone(timezone.utc)
        rows = tuple(_project_record(value, member_id=member_id) for value in values)
        expected = tuple(
            sorted(rows, key=lambda row: (row.created_at, row.record_id), reverse=True)
        )
        if rows != expected:
            raise ValueError("Leadership records are not ordered")
        return LeadershipRecordSnapshot(
            observed_at=observed.isoformat(),
            member_id=member_id,
            records=rows,
        )


def _project_record(
    value: Any,
    *,
    member_id: int | None,
) -> LeadershipRecordRow:
    if not isinstance(value, dict) or value.get("status") != "active":
        raise ValueError("Invalid active leadership record")
    record_id = _positive_int(value.get("id"))
    record_member_id = _positive_int(value.get("member_id"))
    created_at = _timestamp(value.get("created_ts"))
    updated_at = _timestamp(value.get("updated_ts"))
    category_key = value.get("category_key")
    incident_type_key = value.get("incident_type_key")
    category = (
        CATEGORY_BY_KEY.get(category_key)
        if isinstance(category_key, str)
        else None
    )
    incident = (
        INCIDENT_TYPE_BY_KEY.get(incident_type_key)
        if isinstance(incident_type_key, str)
        else None
    )
    strings = {
        "member_display": value.get("member_display"),
        "note": value.get("note"),
        "recorder_display": value.get("recorder_display"),
    }
    if (
        record_id is None
        or record_member_id is None
        or (member_id is not None and record_member_id != member_id)
        or category is None
        or incident is None
        or incident.category_key != category.key
        or any(not isinstance(item, str) or not item.strip() for item in strings.values())
    ):
        raise ValueError("Invalid active leadership record")
    return LeadershipRecordRow(
        record_id=record_id,
        created_at=created_at,
        updated_at=updated_at,
        member_id=record_member_id,
        member_display=strings["member_display"],
        category_key=category.key,
        category_label=category.label,
        incident_type_key=incident.key,
        incident_type_label=incident.label,
        note=strings["note"],
        recorder_display=strings["recorder_display"],
    )


def _positive_int(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _timestamp(value: Any) -> str:
    if type(value) is not int or value < 0:
        raise ValueError("Invalid leadership record timestamp")
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError) as error:
        raise ValueError("Invalid leadership record timestamp") from error


__all__ = [
    "LeadershipRecordQueries",
    "LeadershipRecordRow",
    "LeadershipRecordSnapshot",
    "MAX_ACTIVE_LEADERSHIP_RECORDS",
]
