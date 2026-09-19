"""Retained status-only examination case reports."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from typing import Any

from elbow_helper.features.examination.queries import (
    CASE_TYPES,
    FOLLOWUP_STAGES,
    INTAKE_STATUSES,
    MAX_ACTIVE_EXAMINATION_CASES,
    ExaminationCaseSnapshot,
    ExaminationCaseStatus,
    examination_workflow_status,
)


_EXAM_REQUIREMENTS = {"required", "not_required", "undetermined"}
_ROUTING_STATUSES = {"pending", "in_progress", "routed"}
_RESPONSE_STATUSES = {"recorded", "not_recorded"}


@dataclass(frozen=True, slots=True)
class ExaminationCaseReport:
    report_id: str
    guild_id: int
    snapshot: ExaminationCaseSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.report_id, str)
            or not self.report_id
            or len(self.report_id) > 32
            or type(self.guild_id) is not int
            or self.guild_id <= 0
            or type(self.snapshot.selected_entry_count) is not int
            or not 0 <= self.snapshot.selected_entry_count <= MAX_ACTIVE_EXAMINATION_CASES
            or type(self.snapshot.skipped_invalid_selected_count) is not int
            or self.snapshot.skipped_invalid_selected_count < 0
            or len(self.snapshot.cases) + self.snapshot.skipped_invalid_selected_count
            != self.snapshot.selected_entry_count
        ):
            raise ValueError("Invalid examination case report identity")
        _timestamp(self.snapshot.observed_at)
        channel_ids = [row.ticket_channel_id for row in self.snapshot.cases]
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("Duplicate examination case in report")
        previous = None
        for row in self.snapshot.cases:
            _validate_case(row)
            order = (row.workflow_status, row.ticket_channel_id)
            if previous is not None and previous > order:
                raise ValueError("Examination cases are not ordered")
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
        type_counts = Counter(row.case_type for row in self.snapshot.cases)
        workflow_counts = Counter(row.workflow_status for row in self.snapshot.cases)
        response_counts = Counter(row.response_status for row in self.snapshot.cases)
        return {
            "report_id": self.report_id,
            "kind": "examination_case_status",
            "observed_at": self.snapshot.observed_at,
            "accessible_selected_entry_count": self.snapshot.selected_entry_count,
            "accessible_case_count": len(self.snapshot.cases),
            "skipped_invalid_accessible_count": (
                self.snapshot.skipped_invalid_selected_count
            ),
            "case_type_counts": dict(sorted(type_counts.items())),
            "workflow_status_counts": dict(sorted(workflow_counts.items())),
            "response_status_counts": dict(sorted(response_counts.items())),
            "complete_valid_accessible_status_snapshot": True,
            "message_history_read": False,
            "included_fields": [
                "ticket channel ID",
                "applicant member ID when recorded",
                "promotion case type",
                "intake and routing status",
                "exam requirement",
                "follow-up stage",
                "response-recorded status",
            ],
            "excluded_fields": [
                "ticket and routing messages",
                "application answers",
                "availability",
                "clan choices and Town Hall level",
                "examiner matches and mentions",
                "message IDs",
                "review outcome",
            ],
            "interpretation": (
                "Status is derived only from the examination feature's stored workflow "
                "flags. A recorded response means the feature observed a qualifying "
                "non-applicant ticket message or linked routing response; it does not "
                "prove a question was answered, an exam occurred, or an outcome was decided."
            ),
        }

    def page(
        self,
        *,
        offset: int = 0,
        limit: int = 25,
        ticket_channel_id: int | None = None,
        applicant_member_id: int | None = None,
        case_type: str | None = None,
        workflow_status: str | None = None,
        response_status: str | None = None,
    ) -> dict[str, Any]:
        rows = self.snapshot.cases
        if ticket_channel_id is not None:
            rows = tuple(
                row for row in rows if row.ticket_channel_id == ticket_channel_id
            )
        if applicant_member_id is not None:
            rows = tuple(
                row for row in rows
                if row.applicant_member_id == applicant_member_id
            )
        if case_type is not None:
            rows = tuple(row for row in rows if row.case_type == case_type)
        if workflow_status is not None:
            rows = tuple(
                row for row in rows if row.workflow_status == workflow_status
            )
        if response_status is not None:
            rows = tuple(
                row for row in rows if row.response_status == response_status
            )
        return {
            **self.manifest(),
            "matched_count": len(rows),
            "cases": [asdict(row) for row in rows[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
        }


def _validate_case(row: ExaminationCaseStatus) -> None:
    if (
        type(row.ticket_channel_id) is not int
        or row.ticket_channel_id <= 0
        or row.case_type not in CASE_TYPES
        or row.exam_requirement not in _EXAM_REQUIREMENTS
        or row.routing_status not in _ROUTING_STATUSES
        or row.followup_stage not in FOLLOWUP_STAGES
        or row.response_status not in _RESPONSE_STATUSES
    ):
        raise ValueError("Invalid examination case status")
    if row.applicant_status == "unidentified":
        applicant_valid = row.applicant_member_id is None
    elif row.applicant_status == "identified":
        applicant_valid = (
            type(row.applicant_member_id) is int and row.applicant_member_id > 0
        )
    else:
        applicant_valid = False
    if not applicant_valid:
        raise ValueError("Invalid examination applicant status")
    if row.case_type == "clan_promo":
        intake_valid = row.intake_status in INTAKE_STATUSES
    else:
        intake_valid = row.intake_status == "not_applicable"
    if not intake_valid:
        raise ValueError("Invalid examination intake status")
    expected_workflow = examination_workflow_status(
        case_type=row.case_type,
        intake_status=row.intake_status,
        routing_status=row.routing_status,
        followup_stage=row.followup_stage,
        responded=row.response_status == "recorded",
    )
    if row.workflow_status != expected_workflow:
        raise ValueError("Invalid examination workflow status")


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid examination report timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Invalid examination report timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("Invalid examination report timestamp")
    return parsed


__all__ = ["ExaminationCaseReport"]
