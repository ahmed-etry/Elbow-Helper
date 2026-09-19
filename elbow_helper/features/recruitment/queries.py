"""Status-only reads of active recruitment trials."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import TRIAL_DAYS_DEFAULT


MAX_ACTIVE_RECRUITMENT_TRIALS = 250


@dataclass(frozen=True, slots=True)
class RecruitmentTrialRegistration:
    ticket_channel_id: int


@dataclass(frozen=True, slots=True)
class ActiveRecruitmentTrial:
    ticket_channel_id: int
    applicant_member_id: int
    start_at: str
    start_ts: int
    duration_days: int
    expected_end_at: str
    expected_end_ts: int
    timing_status: str
    remaining_seconds: int | None
    overdue_seconds: int | None


@dataclass(frozen=True, slots=True)
class ActiveRecruitmentTrialSnapshot:
    observed_at: str
    selected_entry_count: int
    skipped_invalid_selected_count: int
    trials: tuple[ActiveRecruitmentTrial, ...]


class RecruitmentQueries:
    """Project recruitment state without exposing tracking or reminder details."""

    def __init__(
        self,
        trial_loader: Callable[[], Mapping[str, Any]],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._trial_loader = trial_loader
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def active_trial_registrations(
        self,
    ) -> tuple[RecruitmentTrialRegistration, ...]:
        data = self._load()
        registrations = []
        for raw_channel_id, value in data.items():
            channel_id = _positive_int(raw_channel_id)
            if channel_id is not None and isinstance(value, Mapping):
                registrations.append(RecruitmentTrialRegistration(channel_id))
        registrations.sort(key=lambda row: row.ticket_channel_id)
        return tuple(registrations)

    def active_trial_snapshot(
        self,
        *,
        ticket_channel_ids: Iterable[int],
    ) -> ActiveRecruitmentTrialSnapshot:
        selected_values = tuple(ticket_channel_ids)
        if (
            len(selected_values) > MAX_ACTIVE_RECRUITMENT_TRIALS
            or any(type(value) is not int or value <= 0 for value in selected_values)
            or len(selected_values) != len(set(selected_values))
        ):
            raise ValueError("Invalid recruitment trial selection")
        selected = frozenset(selected_values)
        data = self._load()
        observed = self._clock()
        if not isinstance(observed, datetime):
            raise ValueError("Invalid recruitment trial observation time")
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        observed = observed.astimezone(timezone.utc)

        selected_entries: dict[int, Any] = {}
        for raw_channel_id, value in data.items():
            channel_id = _positive_int(raw_channel_id)
            if channel_id not in selected:
                continue
            if channel_id in selected_entries:
                raise ValueError("Duplicate recruitment trial channel identity")
            selected_entries[channel_id] = value
        trials = []
        skipped = 0
        for channel_id, value in selected_entries.items():
            trial = _trial(channel_id, value, observed=observed)
            if trial is None:
                skipped += 1
                continue
            trials.append(trial)
        trials.sort(key=lambda row: (row.expected_end_ts, row.ticket_channel_id))
        return ActiveRecruitmentTrialSnapshot(
            observed_at=observed.isoformat(),
            selected_entry_count=len(selected_entries),
            skipped_invalid_selected_count=skipped,
            trials=tuple(trials),
        )

    def _load(self) -> Mapping[str, Any]:
        data = self._trial_loader()
        if not isinstance(data, Mapping):
            raise ValueError("Invalid recruitment trial state")
        if len(data) > MAX_ACTIVE_RECRUITMENT_TRIALS:
            raise ValueError("Recruitment trial state exceeds its read bound")
        return data


def _trial(
    channel_id: int | None,
    value: Any,
    *,
    observed: datetime,
) -> ActiveRecruitmentTrial | None:
    if channel_id is None or not isinstance(value, Mapping):
        return None
    applicant_id = _positive_int(value.get("applicant_id"))
    duration_days = _positive_int(value.get("days", TRIAL_DAYS_DEFAULT))
    start = _timestamp(value.get("start"))
    if applicant_id is None or duration_days is None or start is None:
        return None
    try:
        expected_end = start + timedelta(days=duration_days)
        start_ts = int(start.timestamp())
        expected_end_ts = int(expected_end.timestamp())
    except (OSError, OverflowError, ValueError):
        return None
    if observed >= expected_end:
        timing_status = "due"
        remaining_seconds = None
        overdue_seconds = int((observed - expected_end).total_seconds())
    else:
        timing_status = "in_progress"
        remaining_seconds = int((expected_end - observed).total_seconds())
        overdue_seconds = None
    return ActiveRecruitmentTrial(
        ticket_channel_id=channel_id,
        applicant_member_id=applicant_id,
        start_at=start.isoformat(),
        start_ts=start_ts,
        duration_days=duration_days,
        expected_end_at=expected_end.isoformat(),
        expected_end_ts=expected_end_ts,
        timing_status=timing_status,
        remaining_seconds=remaining_seconds,
        overdue_seconds=overdue_seconds,
    )


def _positive_int(value: Any) -> int | None:
    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


__all__ = [
    "ActiveRecruitmentTrial",
    "ActiveRecruitmentTrialSnapshot",
    "MAX_ACTIVE_RECRUITMENT_TRIALS",
    "RecruitmentQueries",
    "RecruitmentTrialRegistration",
]
