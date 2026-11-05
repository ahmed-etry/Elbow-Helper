"""Closed event vocabulary: identifiers and fixed values, never message text."""

from dataclasses import asdict, dataclass, field
import json
import time

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag


KINDS = {
    "leadership_news": frozenset({"news_published", "voided"}),
    "transfers": frozenset({"transfer_requested", "transfer_cancelled", "transfer_expired", "queue_cleared", "voided"}),
}
DETAILS = {
    "news_published": frozenset({"channel_id", "message_id", "guild_id"}),
    "transfer_requested": frozenset(), "transfer_cancelled": frozenset(),
    "transfer_expired": frozenset({"after_hours"}), "queue_cleared": frozenset({"count"}),
    "voided": frozenset({"voids"}),
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

    @property
    def idem_key(self):
        return f"{self.workflow}:{self.case_key}:{self.kind}"


def _positive(value):
    return type(value) is int and 0 < value < 2**63


def validate(event):
    if event.workflow not in KINDS or event.kind not in KINDS[event.workflow]:
        raise ValueError("Unknown workflow event")
    if type(event.occurred_at) is not int or not 0 <= event.occurred_at < 2**63:
        raise ValueError("Invalid event time")
    if event.actor_kind not in {"member", "agent", "automatic", "import"}:
        raise ValueError("Invalid event actor")
    if event.via not in {"command", "button", "agent", "schedule", "import"}:
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
    expected = 1 if event.workflow == "leadership_news" else 3
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
    if set(event.detail) != DETAILS[event.kind]:
        raise ValueError("Invalid event details")
    for key, value in event.detail.items():
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
    values["detail_json"] = json.dumps(values.pop("detail"), sort_keys=True, separators=(",", ":"))
    values.update(recorded_at=now_us(), idem_key=event.idem_key)
    columns = tuple(values)
    result = connection.execute(
        f"INSERT INTO workflow_events ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
        "ON CONFLICT(idem_key) DO NOTHING", tuple(values.values()))
    return result.rowcount == 1
