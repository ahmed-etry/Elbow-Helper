"""Retained status-only active recruitment trial reports."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import json
from typing import Any

from elbow_helper.features.recruitment.queries import (
    ActiveRecruitmentTrial,
    ActiveRecruitmentTrialSnapshot,
    MAX_ACTIVE_RECRUITMENT_TRIALS,
)


@dataclass(frozen=True, slots=True)
class RecruitmentTrialReport:
    report_id: str
    guild_id: int
    snapshot: ActiveRecruitmentTrialSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.report_id, str)
            or not self.report_id
            or len(self.report_id) > 32
            or type(self.guild_id) is not int
            or self.guild_id <= 0
            or type(self.snapshot.selected_entry_count) is not int
            or not 0 <= self.snapshot.selected_entry_count <= MAX_ACTIVE_RECRUITMENT_TRIALS
            or type(self.snapshot.skipped_invalid_selected_count) is not int
            or self.snapshot.skipped_invalid_selected_count < 0
            or len(self.snapshot.trials)
            + self.snapshot.skipped_invalid_selected_count
            != self.snapshot.selected_entry_count
        ):
            raise ValueError("Invalid recruitment trial report identity")
        observed = _timestamp(self.snapshot.observed_at)
        channel_ids = [row.ticket_channel_id for row in self.snapshot.trials]
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("Duplicate recruitment trial in report")
        previous = None
        for row in self.snapshot.trials:
            _validate_trial(row, observed=observed)
            order = (row.expected_end_ts, row.ticket_channel_id)
            if previous is not None and previous > order:
                raise ValueError("Recruitment trials are not ordered")
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
        timing_counts = Counter(row.timing_status for row in self.snapshot.trials)
        return {
            "report_id": self.report_id,
            "kind": "active_recruitment_trials",
            "observed_at": self.snapshot.observed_at,
            "accessible_selected_entry_count": self.snapshot.selected_entry_count,
            "accessible_active_trial_count": len(self.snapshot.trials),
            "skipped_invalid_accessible_count": (
                self.snapshot.skipped_invalid_selected_count
            ),
            "timing_status_counts": dict(sorted(timing_counts.items())),
            "complete_valid_accessible_status_snapshot": True,
            "message_history_read": False,
            "included_fields": [
                "ticket channel ID",
                "applicant member ID",
                "trial start",
                "configured duration",
                "expected end",
                "computed timing status",
            ],
            "excluded_fields": [
                "ticket messages",
                "application answers",
                "tracking and reminder message IDs",
                "recruiter notes",
                "trial outcome",
            ],
            "interpretation": (
                "These are stored active trial records. A due expected end does not "
                "prove the trial is unresolved, decide its outcome, or show whether "
                "a reminder or follow-up was sent."
            ),
        }

    def page(
        self,
        *,
        offset: int = 0,
        limit: int = 25,
        ticket_channel_id: int | None = None,
        applicant_member_id: int | None = None,
        timing_status: str | None = None,
    ) -> dict[str, Any]:
        rows = self.snapshot.trials
        if ticket_channel_id is not None:
            rows = tuple(
                row for row in rows if row.ticket_channel_id == ticket_channel_id
            )
        if applicant_member_id is not None:
            rows = tuple(
                row for row in rows
                if row.applicant_member_id == applicant_member_id
            )
        if timing_status is not None:
            rows = tuple(row for row in rows if row.timing_status == timing_status)
        return {
            **self.manifest(),
            "matched_count": len(rows),
            "trials": [asdict(row) for row in rows[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
        }


def _validate_trial(row: ActiveRecruitmentTrial, *, observed: datetime) -> None:
    if (
        type(row.ticket_channel_id) is not int
        or row.ticket_channel_id <= 0
        or type(row.applicant_member_id) is not int
        or row.applicant_member_id <= 0
        or type(row.duration_days) is not int
        or row.duration_days <= 0
        or type(row.start_ts) is not int
        or row.start_ts <= 0
        or type(row.expected_end_ts) is not int
        or row.expected_end_ts <= row.start_ts
    ):
        raise ValueError("Invalid recruitment trial metadata")
    start = _timestamp(row.start_at)
    expected_end = _timestamp(row.expected_end_at)
    try:
        calculated_end = start + timedelta(days=row.duration_days)
    except OverflowError as error:
        raise ValueError("Invalid recruitment trial duration") from error
    try:
        timestamps_valid = (
            int(start.timestamp()) == row.start_ts
            and int(expected_end.timestamp()) == row.expected_end_ts
        )
    except (OSError, OverflowError, ValueError) as error:
        raise ValueError("Invalid recruitment trial timestamps") from error
    if not timestamps_valid or calculated_end != expected_end:
        raise ValueError("Invalid recruitment trial timestamps")
    if row.timing_status == "due":
        timing_valid = (
            observed >= expected_end
            and row.remaining_seconds is None
            and type(row.overdue_seconds) is int
            and row.overdue_seconds
            == int((observed - expected_end).total_seconds())
        )
    elif row.timing_status == "in_progress":
        timing_valid = (
            observed < expected_end
            and type(row.remaining_seconds) is int
            and row.remaining_seconds
            == int((expected_end - observed).total_seconds())
            and row.overdue_seconds is None
        )
    else:
        timing_valid = False
    if not timing_valid:
        raise ValueError("Invalid recruitment trial timing metadata")


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid recruitment trial timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Invalid recruitment trial timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("Invalid recruitment trial timestamp")
    return parsed


__all__ = ["RecruitmentTrialReport"]
