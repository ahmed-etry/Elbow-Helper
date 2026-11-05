"""Closed event vocabulary: identifiers and fixed values, never message text."""

from dataclasses import asdict, dataclass, field
import json
import time

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.recruitment import PLATFORMS
from elbow_helper.domain.player_tags import normalize_player_tag


KINDS = {
    # Server stay times live in the family record; these carry only what it cannot know.
    "lifecycle": frozenset({"joined", "left", "voided"}),
    "leadership_news": frozenset({"news_published", "voided"}),
    "transfers": frozenset({"transfer_requested", "transfer_cancelled", "transfer_expired", "queue_cleared", "voided"}),
    "support": frozenset({"ticket_opened", "ticket_closed", "ticket_reopened", "ticket_deleted", "voided"}),
    "hibernation": frozenset({"hibernated", "reactivated", "voided"}),
    "recruitment": frozenset({"trial_started", "trial_ended", "trial_followup_resolved", "accepted",
                              "declined", "finalized", "checkup_sent", "voided"}),
    # Stages only: no pass or fail outcome is recorded.
    "examination": frozenset({"started", "answered", "availability_given", "sent_for_review",
                              "followed_up", "reviewed", "route_changed", "closed", "voided"}),
}
TRIAL_ENDINGS = frozenset({"early", "expired", "applicant_left"})
EXAMINATION_CASE_TYPES = frozenset({"clan_promo", "elder_promo"})
EXAMINATION_FOLLOWUPS = frozenset({"reminder", "fallback"})
FLAGS = frozenset({"forced", "exam_required", "missing_answers", "reopened"})
LEAVE_REASONS = frozenset({"left", "kicked", "banned"})
DETAILS = {
    "joined": frozenset({"platform"}), "left": frozenset({"reason"}),
    "news_published": frozenset({"channel_id", "message_id", "guild_id"}),
    "transfer_requested": frozenset(), "transfer_cancelled": frozenset(),
    "transfer_expired": frozenset({"after_hours"}), "queue_cleared": frozenset({"count"}),
    "voided": frozenset({"voids"}),
    "hibernated": frozenset({"roles_taken"}),
    "reactivated": frozenset({"forced", "ticket_channel_id"}),
    **{kind: frozenset({"source"}) for kind in
       ("ticket_opened", "ticket_closed", "ticket_reopened", "ticket_deleted")},
    "trial_started": frozenset({"days"}), "trial_ended": frozenset({"how"}),
    "accepted": frozenset({"clans", "trial_days"}),
    **{kind: frozenset() for kind in ("trial_followup_resolved", "declined", "finalized", "checkup_sent")},
    "started": frozenset({"case_type"}), "answered": frozenset({"exam_required"}),
    "sent_for_review": frozenset({"exam_required", "missing_answers"}),
    "followed_up": frozenset({"stage"}), "route_changed": frozenset({"reopened"}),
    **{kind: frozenset() for kind in ("availability_given", "reviewed", "closed")},
}


def now_us():
    return time.time_ns() // 1000


@dataclass(frozen=True)
class Event:
    workflow: str
    case_key: str
    kind: str
    occurred_at: int
    actor_kind: str = "automatic"
    via: str = "schedule"
    subject_discord_id: int | None = None
    subject_tag: str | None = None
    clan_code: str | None = None
    actor_discord_id: int | None = None
    detail: dict = field(default_factory=dict)
    source_channel_id: int | None = None
    source_message_id: int | None = None
    sequence: int = 0

    @property
    def idem_key(self):
        suffix = f":{self.sequence}" if self.sequence else ""
        return f"{self.workflow}:{self.case_key}:{self.kind}{suffix}"


def _positive(value):
    return type(value) is int and 0 < value < 2**63


def validate(event):
    if event.workflow not in KINDS or event.kind not in KINDS[event.workflow]:
        raise ValueError("Unknown workflow event")
    if type(event.occurred_at) is not int or not 0 <= event.occurred_at < 2**63:
        raise ValueError("Invalid event time")
    if event.actor_kind not in {"member", "agent", "automatic", "import"}:
        raise ValueError("Invalid event actor")
    if event.via not in {"command", "button", "message", "agent", "schedule", "import"}:
        raise ValueError("Invalid event route")
    if (event.actor_kind == "import") != (event.via == "import"):
        raise ValueError("Invalid import origin")
    for value in (event.subject_discord_id, event.actor_discord_id,
                  event.source_channel_id, event.source_message_id):
        if value is not None and not _positive(value):
            raise ValueError("Invalid event identifier")
    if event.actor_kind in {"member", "agent"} and event.actor_discord_id is None:
        raise ValueError("Missing event actor identifier")
    if event.clan_code is not None and event.clan_code not in CLANS:
        raise ValueError("Unknown event clan")
    if event.subject_tag is not None and normalize_player_tag(event.subject_tag) != event.subject_tag:
        raise ValueError("Invalid event player tag")
    # Case keys contain only numeric identities and a configured clan code.
    parts = event.case_key.split(":")
    # Recruitment cases are a ticket channel and the case's start time; lifecycle
    # cases are a member and Discord's join time, which names their server stay.
    expected = {"transfers": 3, "hibernation": 2, "recruitment": 2, "lifecycle": 2}.get(event.workflow, 1)
    if len(parts) != expected:
        raise ValueError("Invalid event case")
    for index, part in enumerate(parts):
        if event.workflow == "transfers" and index == 1:
            if part not in CLANS:
                raise ValueError("Invalid event case clan")
        elif not part.isascii() or not part.isdecimal() or not 0 < int(part) < 2**63:
            raise ValueError("Invalid event case identifier")
    if event.workflow == "transfers" and (
            str(event.subject_discord_id) != parts[0] or event.clan_code != parts[1]):
        raise ValueError("Transfer case must match its subject and clan")
    if event.workflow in {"hibernation", "lifecycle"} and str(event.subject_discord_id) != parts[0]:
        raise ValueError("Member case must match its subject")
    if set(event.detail) != DETAILS[event.kind]:
        raise ValueError("Invalid event details")
    if (type(event.sequence) is not int or not 0 <= event.sequence < 2**63
            or (event.sequence and event.workflow not in {"support", "examination"})):
        raise ValueError("Invalid event sequence")
    for key, value in event.detail.items():
        if key == "roles_taken":
            if not isinstance(value, list) or any(not _positive(role_id) for role_id in value):
                raise ValueError("Invalid hibernation roles")
            continue
        if key in FLAGS:
            if type(value) is not bool:
                raise ValueError("Invalid event flag")
            continue
        if key == "case_type":
            if not isinstance(value, str) or value not in EXAMINATION_CASE_TYPES:
                raise ValueError("Invalid examination case type")
            continue
        if key == "stage":
            if not isinstance(value, str) or value not in EXAMINATION_FOLLOWUPS:
                raise ValueError("Invalid examination follow-up")
            continue
        if key == "source":
            if value not in {"open", "reactivation"}:
                raise ValueError("Invalid ticket source")
            continue
        if key == "how":
            if value not in TRIAL_ENDINGS:
                raise ValueError("Invalid trial ending")
            continue
        if key == "platform":
            if not isinstance(value, str) or value not in PLATFORMS:
                raise ValueError("Invalid recruitment source")
            continue
        if key == "reason":
            if value not in LEAVE_REASONS:
                raise ValueError("Invalid leave reason")
            continue
        if key == "clans":
            if (not isinstance(value, list) or not 1 <= len(value) <= 7 or len(set(value)) != len(value)
                    or any(not isinstance(code, str) or code not in CLANS for code in value)):
                raise ValueError("Invalid accepted clans")
            continue
        if type(value) is not int or value < (0 if key == "count" else 1) or value >= 2**63:
            raise ValueError("Invalid event detail value")
    if event.kind == "transfer_expired" and event.detail["after_hours"] != 12:
        raise ValueError("Invalid transfer expiry")


def record(connection, event):
    """Append within the caller's state transaction; a retry is a no-op."""
    if not connection.in_transaction:
        raise RuntimeError("Events require a state transaction")
    validate(event)
    values = asdict(event)
    values.pop("sequence")
    values["detail_json"] = json.dumps(values.pop("detail"), sort_keys=True, separators=(",", ":"))
    values.update(recorded_at=now_us(), idem_key=event.idem_key)
    columns = tuple(values)
    result = connection.execute(
        f"INSERT INTO workflow_events ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
        "ON CONFLICT(idem_key) DO NOTHING", tuple(values.values()))
    return result.rowcount == 1
