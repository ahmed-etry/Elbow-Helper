"""Typed, read-only projections of configured event trackers."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import discord


class EventPhase(Protocol):
    def __call__(self, event: dict[str, Any], now: datetime) -> str: ...


class RecurringRange(Protocol):
    def __call__(
        self, event: dict[str, Any], reference: datetime,
    ) -> tuple[datetime, datetime]: ...


class RecurringPoint(Protocol):
    def __call__(self, event: dict[str, Any], reference: datetime) -> datetime: ...


@dataclass(frozen=True, slots=True)
class EventScheduleRow:
    event_key: str
    name: str
    source: str
    event_type: str
    schedule_shape: str
    enabled: bool
    phase: str
    position: int
    timezone: str | None
    grace_period_hours: int
    start_at: str | None
    end_at: str | None
    next_at: str | None
    member_count: int | None
    configured_role_count: int
    missing_role_count: int
    count_coverage: str | None


@dataclass(frozen=True, slots=True)
class EventScheduleSnapshot:
    observed_at: str
    rows: tuple[EventScheduleRow, ...]


class EventStatsQueries:
    """Project existing event state without exposing its management methods."""

    def __init__(
        self,
        events_source: Callable[[], Sequence[dict[str, Any]]],
        *,
        event_phase: EventPhase,
        recurring_range: RecurringRange,
        recurring_point: RecurringPoint,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._events_source = events_source
        self._event_phase = event_phase
        self._recurring_range = recurring_range
        self._recurring_point = recurring_point
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def snapshot(self, guild: discord.Guild) -> EventScheduleSnapshot:
        observed = self._clock()
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        observed = observed.astimezone(timezone.utc)
        rows = tuple(
            self._project(dict(event), guild, observed)
            for event in self._events_source()
        )
        return EventScheduleSnapshot(observed.isoformat(), tuple(sorted(
            rows, key=lambda row: (row.position, row.event_key),
        )))

    def _project(
        self, event: dict[str, Any], guild: discord.Guild,
        observed: datetime,
    ) -> EventScheduleRow:
        event_type = str(event.get("type") or "")
        if event_type not in {"counter", "recurring", "one-time"}:
            raise ValueError("Invalid event tracker type")
        schedule_shape = (
            str(event.get("schedule_shape") or "range")
            if event_type == "recurring" else event_type
        )
        if schedule_shape not in {"counter", "range", "point", "one-time"}:
            raise ValueError("Invalid event schedule shape")
        start_at = end_at = next_at = None
        member_count = None
        configured_role_count = missing_role_count = 0
        count_coverage = None
        if event_type == "counter":
            role_ids = tuple({
                int(value) for value in event.get("roles_to_count") or ()
                if type(value) is int and value > 0
            })
            roles = tuple(
                role for role_id in role_ids
                if (role := guild.get_role(role_id)) is not None
            )
            configured_role_count = len(role_ids)
            missing_role_count = configured_role_count - len(roles)
            member_count = len({
                member.id for role in roles for member in role.members
            })
            count_coverage = (
                "complete" if missing_role_count == 0
                else "partial_missing_roles"
            )
        elif event_type == "one-time":
            start_at = _utc_iso(event.get("start"))
            end_at = _utc_iso(event.get("end"))
        elif schedule_shape == "point":
            next_at = _utc_iso(self._recurring_point(event, observed))
        else:
            start, end = self._recurring_range(event, observed)
            start_at, end_at = _utc_iso(start), _utc_iso(end)
        return EventScheduleRow(
            event_key=str(event.get("key") or ""),
            name=str(event.get("name") or ""),
            source=str(event.get("source") or ""),
            event_type=event_type, schedule_shape=schedule_shape,
            enabled=bool(event.get("enabled", True)),
            phase=self._event_phase(event, observed),
            position=int(event.get("position") or 0),
            timezone=(
                str(event.get("timezone") or "UTC")
                if event_type == "one-time" else None
            ),
            grace_period_hours=int(event.get("grace_period_hours") or 0),
            start_at=start_at, end_at=end_at, next_at=next_at,
            member_count=member_count,
            configured_role_count=configured_role_count,
            missing_role_count=missing_role_count,
            count_coverage=count_coverage,
        )


def _utc_iso(value: Any) -> str:
    if not isinstance(value, datetime):
        raise ValueError("Invalid event schedule time")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


__all__ = ["EventScheduleRow", "EventScheduleSnapshot", "EventStatsQueries"]
