"""Status-only reads of active examination cases."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


MAX_ACTIVE_EXAMINATION_CASES = 250
CASE_TYPES = frozenset({"clan_promo", "elder_promo"})
INTAKE_STATUSES = frozenset({
    "pending", "selecting_from", "selecting_to", "th_recovery", "confirming",
    "complete",
})
FOLLOWUP_STAGES = frozenset({"pending", "initial", "reminder", "fallback", "missing"})
WORKFLOW_STATUSES = frozenset({
    "awaiting_applicant_intake",
    "routing_in_progress",
    "awaiting_routing",
    "response_recorded",
    "missing_application_fields",
    "fallback_followup_sent",
    "followup_sent",
    "awaiting_response",
})


@dataclass(frozen=True, slots=True)
class ExaminationCaseRegistration:
    ticket_channel_id: int


@dataclass(frozen=True, slots=True)
class ExaminationCaseStatus:
    ticket_channel_id: int
    applicant_member_id: int | None
    applicant_status: str
    case_type: str
    intake_status: str
    exam_requirement: str
    routing_status: str
    followup_stage: str
    response_status: str
    workflow_status: str


@dataclass(frozen=True, slots=True)
class ExaminationCaseSnapshot:
    observed_at: str
    selected_entry_count: int
    skipped_invalid_selected_count: int
    cases: tuple[ExaminationCaseStatus, ...]


class ExaminationQueries:
    """Project case status without application, availability or routing details."""

    def __init__(
        self,
        cases: Callable[[], Mapping[str, Any]],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._cases = cases
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def case_registrations(self) -> tuple[ExaminationCaseRegistration, ...]:
        cases = self._load()
        registrations = []
        for raw_channel_id, value in cases.items():
            channel_id = _positive_int(raw_channel_id)
            if channel_id is not None and isinstance(value, Mapping):
                registrations.append(ExaminationCaseRegistration(channel_id))
        registrations.sort(key=lambda row: row.ticket_channel_id)
        return tuple(registrations)

    def case_snapshot(
        self,
        *,
        ticket_channel_ids: Iterable[int],
    ) -> ExaminationCaseSnapshot:
        selected_values = tuple(ticket_channel_ids)
        if (
            len(selected_values) > MAX_ACTIVE_EXAMINATION_CASES
            or any(type(value) is not int or value <= 0 for value in selected_values)
            or len(selected_values) != len(set(selected_values))
        ):
            raise ValueError("Invalid examination case selection")
        selected = frozenset(selected_values)
        cases = self._load()
        observed = self._clock()
        if not isinstance(observed, datetime):
            raise ValueError("Invalid examination observation time")
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        observed = observed.astimezone(timezone.utc)

        selected_entries: dict[int, Any] = {}
        for raw_channel_id, value in cases.items():
            channel_id = _positive_int(raw_channel_id)
            if channel_id not in selected:
                continue
            if channel_id in selected_entries:
                raise ValueError("Duplicate examination case channel identity")
            selected_entries[channel_id] = value
        rows = []
        skipped = 0
        for channel_id, value in selected_entries.items():
            row = _case_status(channel_id, value)
            if row is None:
                skipped += 1
                continue
            rows.append(row)
        rows.sort(key=lambda row: (row.workflow_status, row.ticket_channel_id))
        return ExaminationCaseSnapshot(
            observed_at=observed.isoformat(),
            selected_entry_count=len(selected_entries),
            skipped_invalid_selected_count=skipped,
            cases=tuple(rows),
        )

    def _load(self) -> Mapping[str, Any]:
        cases = self._cases()
        if not isinstance(cases, Mapping):
            raise ValueError("Invalid examination case state")
        if len(cases) > MAX_ACTIVE_EXAMINATION_CASES:
            raise ValueError("Examination case state exceeds its read bound")
        return cases


def _case_status(
    channel_id: int,
    value: Any,
) -> ExaminationCaseStatus | None:
    if not isinstance(value, Mapping):
        return None
    stored_channel_id = _positive_int(value.get("ticket_channel_id"))
    case_type = value.get("type")
    if (
        stored_channel_id != channel_id
        or not isinstance(case_type, str)
        or case_type not in CASE_TYPES
    ):
        return None
    applicant_id = _positive_int(value.get("opener_id"))
    raw_applicant_id = value.get("opener_id")
    if raw_applicant_id is not None and applicant_id is None:
        return None
    applicant_status = "identified" if applicant_id is not None else "unidentified"

    if case_type == "clan_promo":
        intake_status = value.get("intake_state", "pending")
        if not isinstance(intake_status, str) or intake_status not in INTAKE_STATUSES:
            return None
    else:
        intake_status = "not_applicable"

    exam_required = value.get("exam_required")
    if exam_required is None:
        exam_requirement = "undetermined"
    elif type(exam_required) is bool:
        exam_requirement = "required" if exam_required else "not_required"
    else:
        return None

    routing_inflight = value.get("routing_inflight", False)
    if type(routing_inflight) is not bool:
        return None
    routing_message_id = value.get("routing_message_id")
    if routing_message_id is not None and _positive_int(routing_message_id) is None:
        return None
    if routing_inflight:
        routing_status = "in_progress"
    elif routing_message_id is not None:
        routing_status = "routed"
    else:
        routing_status = "pending"

    followup_stage = value.get("stage", "pending")
    responded = value.get("responded", False)
    if (
        not isinstance(followup_stage, str)
        or followup_stage not in FOLLOWUP_STAGES
        or type(responded) is not bool
    ):
        return None
    response_status = "recorded" if responded else "not_recorded"
    workflow_status = examination_workflow_status(
        case_type=case_type,
        intake_status=intake_status,
        routing_status=routing_status,
        followup_stage=followup_stage,
        responded=responded,
    )
    return ExaminationCaseStatus(
        ticket_channel_id=channel_id,
        applicant_member_id=applicant_id,
        applicant_status=applicant_status,
        case_type=case_type,
        intake_status=intake_status,
        exam_requirement=exam_requirement,
        routing_status=routing_status,
        followup_stage=followup_stage,
        response_status=response_status,
        workflow_status=workflow_status,
    )


def examination_workflow_status(
    *,
    case_type: str,
    intake_status: str,
    routing_status: str,
    followup_stage: str,
    responded: bool,
) -> str:
    if case_type == "clan_promo" and intake_status != "complete":
        return "awaiting_applicant_intake"
    if routing_status == "in_progress":
        return "routing_in_progress"
    if routing_status == "pending":
        return "awaiting_routing"
    if responded:
        return "response_recorded"
    if followup_stage == "missing":
        return "missing_application_fields"
    if followup_stage == "fallback":
        return "fallback_followup_sent"
    if followup_stage == "reminder":
        return "followup_sent"
    return "awaiting_response"


def _positive_int(value: Any) -> int | None:
    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


__all__ = [
    "CASE_TYPES",
    "ExaminationCaseRegistration",
    "ExaminationCaseSnapshot",
    "ExaminationCaseStatus",
    "ExaminationQueries",
    "FOLLOWUP_STAGES",
    "INTAKE_STATUSES",
    "MAX_ACTIVE_EXAMINATION_CASES",
    "WORKFLOW_STATUSES",
    "examination_workflow_status",
]
