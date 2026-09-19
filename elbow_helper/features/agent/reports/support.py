"""Retained metadata-only support ticket inventory."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from typing import Any

from elbow_helper.features.support_tickets.queries import (
    MAX_SUPPORT_TICKET_CHANNELS,
    SupportTicketMetadata,
    SupportTicketSnapshot,
)


@dataclass(frozen=True, slots=True)
class SupportTicketReport:
    report_id: str
    guild_id: int
    registered_ticket_count: int
    omitted_inaccessible_count: int
    snapshot: SupportTicketSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.report_id, str) or not self.report_id
            or len(self.report_id) > 32 or type(self.guild_id) is not int
            or self.guild_id <= 0
            or type(self.registered_ticket_count) is not int
            or not 0 <= self.registered_ticket_count <= MAX_SUPPORT_TICKET_CHANNELS
            or type(self.omitted_inaccessible_count) is not int
            or self.omitted_inaccessible_count < 0
            or len(self.snapshot.tickets) + self.omitted_inaccessible_count
            != self.registered_ticket_count
        ):
            raise ValueError("Invalid support ticket report identity")
        observed = _timestamp(self.snapshot.observed_at)
        ids = [row.channel_id for row in self.snapshot.tickets]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate support ticket in report")
        previous = None
        for row in self.snapshot.tickets:
            _validate_ticket(row, observed=observed)
            order = (
                row.last_activity_ts is not None,
                row.last_activity_ts or 0, row.created_ts, row.channel_id,
            )
            if previous is not None and previous > order:
                raise ValueError("Support tickets are not ordered")
            previous = order
        object.__setattr__(self, "retained_bytes", len(json.dumps(
            self.storage_payload(), ensure_ascii=False,
        ).encode("utf-8")))

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "guild_id": self.guild_id,
            "registered_ticket_count": self.registered_ticket_count,
            "omitted_inaccessible_count": self.omitted_inaccessible_count,
            "snapshot": asdict(self.snapshot),
        }

    def manifest(self) -> dict[str, Any]:
        owner_counts = Counter(row.owner_status for row in self.snapshot.tickets)
        send_counts = Counter(
            "unknown" if row.owner_can_send is None
            else "can_send" if row.owner_can_send else "cannot_send"
            for row in self.snapshot.tickets
        )
        known_activity = [
            row for row in self.snapshot.tickets
            if row.last_activity_ts is not None
        ]
        return {
            "report_id": self.report_id, "kind": "support_ticket_inventory",
            "observed_at": self.snapshot.observed_at,
            "registered_ticket_count": self.registered_ticket_count,
            "accessible_ticket_count": len(self.snapshot.tickets),
            "omitted_inaccessible_count": self.omitted_inaccessible_count,
            "owner_status_counts": dict(sorted(owner_counts.items())),
            "owner_send_status_counts": dict(sorted(send_counts.items())),
            "tickets_without_last_message_id": sum(
                row.activity_status == "no_message_id"
                for row in self.snapshot.tickets
            ),
            "oldest_known_activity_at": (
                min(known_activity, key=lambda row: row.last_activity_ts).last_activity_at
                if known_activity else None
            ),
            "complete_accessible_metadata_snapshot": True,
            "message_history_read": False,
            "included_fields": [
                "channel identity", "topic-derived owner ID",
                "owner send permission", "creation time", "last-message time",
            ],
            "excluded_fields": [
                "message content", "message authors", "attachments", "ticket topic text",
            ],
            "interpretation": (
                "Last activity is derived from the channel's last-message ID and does not "
                "identify a meaningful reply, who should respond next, whether a question "
                "was answered, or whether the ticket is stale under an approved policy."
            ),
        }

    def page(
        self, *, offset: int = 0, limit: int = 25,
        channel_id: int | None = None, owner_member_id: int | None = None,
        owner_can_send: bool | None = None, activity_status: str | None = None,
    ) -> dict[str, Any]:
        rows = self.snapshot.tickets
        if channel_id is not None:
            rows = tuple(row for row in rows if row.channel_id == channel_id)
        if owner_member_id is not None:
            rows = tuple(row for row in rows if row.owner_member_id == owner_member_id)
        if owner_can_send is not None:
            rows = tuple(row for row in rows if row.owner_can_send is owner_can_send)
        if activity_status is not None:
            rows = tuple(row for row in rows if row.activity_status == activity_status)
        return {
            **self.manifest(), "matched_count": len(rows),
            "tickets": [asdict(row) for row in rows[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
        }


def _validate_ticket(row: SupportTicketMetadata, *, observed: datetime) -> None:
    if (
        type(row.channel_id) is not int or row.channel_id <= 0
        or not isinstance(row.channel_name, str) or not row.channel_name
        or len(row.channel_name) > 100
        or type(row.created_ts) is not int or row.created_ts <= 0
        or int(_timestamp(row.created_at).timestamp()) != row.created_ts
    ):
        raise ValueError("Invalid support ticket metadata")
    if row.owner_status == "unidentified":
        owner_valid = row.owner_member_id is None and row.owner_can_send is None
    elif row.owner_status == "not_in_guild_cache":
        owner_valid = (
            type(row.owner_member_id) is int and row.owner_member_id > 0
            and row.owner_can_send is None
        )
    elif row.owner_status == "resolved":
        owner_valid = (
            type(row.owner_member_id) is int and row.owner_member_id > 0
            and type(row.owner_can_send) is bool
        )
    else:
        owner_valid = False
    if not owner_valid:
        raise ValueError("Invalid support ticket owner metadata")
    if row.activity_status == "no_message_id":
        activity_valid = all(value is None for value in (
            row.last_activity_at, row.last_activity_ts,
            row.last_activity_age_seconds,
        ))
    elif row.activity_status == "channel_last_message":
        last_activity = _timestamp(row.last_activity_at)
        activity_valid = (
            type(row.last_activity_ts) is int and row.last_activity_ts > 0
            and int(last_activity.timestamp()) == row.last_activity_ts
            and type(row.last_activity_age_seconds) is int
            and row.last_activity_age_seconds == max(
                0, int((observed - last_activity).total_seconds())
            )
        )
    else:
        activity_valid = False
    if not activity_valid:
        raise ValueError("Invalid support ticket activity metadata")


def _timestamp(value: str | None) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid support ticket timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Invalid support ticket timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("Invalid support ticket timestamp")
    return parsed


__all__ = ["SupportTicketReport"]
