"""Retained permission-filtered clan-transfer queue snapshots."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from typing import Any

from elbow_helper.features.clan_transfers.config import CLAN_TRANSFER_QUEUES
from elbow_helper.features.clan_transfers.queries import (
    PendingTransferRequest,
    TransferQueueSnapshot,
)


@dataclass(frozen=True, slots=True)
class TransferQueueReport:
    report_id: str
    guild_id: int
    observed_at: str
    request_ttl_hours: int
    registered_queue_count: int
    omitted_inaccessible_count: int
    queues: tuple[TransferQueueSnapshot, ...]
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.report_id, str) or not self.report_id
            or len(self.report_id) > 32 or type(self.guild_id) is not int
            or self.guild_id <= 0
            or type(self.request_ttl_hours) is not int
            or not 1 <= self.request_ttl_hours <= 720
            or type(self.registered_queue_count) is not int
            or not 0 <= self.registered_queue_count <= len(CLAN_TRANSFER_QUEUES)
            or type(self.omitted_inaccessible_count) is not int
            or self.omitted_inaccessible_count < 0
            or len(self.queues) + self.omitted_inaccessible_count
            != self.registered_queue_count
        ):
            raise ValueError("Invalid transfer queue report identity")
        observed = _timestamp(self.observed_at)
        codes = [queue.clan_code for queue in self.queues]
        if len(codes) != len(set(codes)):
            raise ValueError("Duplicate transfer queue in report")
        for queue in self.queues:
            config = CLAN_TRANSFER_QUEUES.get(queue.clan_code)
            if (
                config is None or type(queue.thread_id) is not int
                or queue.thread_id != config["thread_id"]
                or type(queue.stored_request_count) is not int
                or type(queue.expired_stored_count) is not int
                or queue.expired_stored_count < 0
                or queue.stored_request_count != (
                    queue.expired_stored_count + len(queue.pending)
                )
            ):
                raise ValueError("Invalid transfer queue coverage")
            member_ids = [row.member_id for row in queue.pending]
            if len(member_ids) != len(set(member_ids)):
                raise ValueError("Duplicate member in transfer queue")
            previous = None
            for request in queue.pending:
                _validate_request(
                    request, observed=observed,
                    ttl_seconds=self.request_ttl_hours * 3600,
                )
                order = (request.created_ts, request.member_id)
                if previous is not None and previous > order:
                    raise ValueError("Transfer requests are not ordered")
                previous = order
        object.__setattr__(self, "retained_bytes", len(json.dumps(
            self.storage_payload(), ensure_ascii=False,
        ).encode("utf-8")))

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "guild_id": self.guild_id,
            "observed_at": self.observed_at,
            "request_ttl_hours": self.request_ttl_hours,
            "registered_queue_count": self.registered_queue_count,
            "omitted_inaccessible_count": self.omitted_inaccessible_count,
            "queues": [asdict(queue) for queue in self.queues],
        }

    def manifest(self) -> dict[str, Any]:
        summaries = []
        for queue in self.queues:
            summaries.append({
                "clan_code": queue.clan_code,
                "thread_id": queue.thread_id,
                "url": (
                    f"https://discord.com/channels/{self.guild_id}/{queue.thread_id}"
                ),
                "pending_count": len(queue.pending),
                "stored_request_count": queue.stored_request_count,
                "expired_stored_count": queue.expired_stored_count,
                "oldest_created_at": (
                    queue.pending[0].created_at if queue.pending else None
                ),
                "next_expiry_at": (
                    min(queue.pending, key=lambda row: row.expires_ts).expires_at
                    if queue.pending else None
                ),
            })
        return {
            "report_id": self.report_id, "kind": "pending_transfer_requests",
            "observed_at": self.observed_at,
            "request_ttl_hours": self.request_ttl_hours,
            "registered_queue_count": self.registered_queue_count,
            "accessible_queue_count": len(self.queues),
            "omitted_inaccessible_count": self.omitted_inaccessible_count,
            "pending_request_count": sum(len(queue.pending) for queue in self.queues),
            "expired_stored_count": sum(
                queue.expired_stored_count for queue in self.queues
            ),
            "queue_summaries": summaries,
            "complete_accessible_queue_snapshot": True,
            "interpretation": (
                "Pending rows are unresolved requests still within the queue's expiry "
                "window at observed_at. They claim a destination, not approval or a "
                "completed in-game transfer. Expired stored rows are counted but omitted; "
                "cleared and expired request history is not retained by this feature."
            ),
        }

    def page(
        self, *, offset: int = 0, limit: int = 25,
        clan_code: str | None = None, member_id: int | None = None,
    ) -> dict[str, Any]:
        rows = [
            {
                "clan_code": queue.clan_code,
                "thread_id": queue.thread_id,
                **asdict(request),
            }
            for queue in self.queues for request in queue.pending
            if (clan_code is None or queue.clan_code == clan_code)
            and (member_id is None or request.member_id == member_id)
        ]
        rows.sort(key=lambda row: (
            row["created_ts"], row["clan_code"], row["member_id"],
        ))
        return {
            **self.manifest(), "matched_count": len(rows),
            "requests": rows[offset:offset + limit],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
        }


def _validate_request(
    request: PendingTransferRequest, *, observed: datetime, ttl_seconds: int,
) -> None:
    if type(request.member_id) is not int or request.member_id <= 0:
        raise ValueError("Invalid pending transfer member")
    created = _timestamp(request.created_at)
    expires = _timestamp(request.expires_at)
    if (
        type(request.created_ts) is not int
        or type(request.expires_ts) is not int
        or request.created_ts != int(created.timestamp())
        or request.expires_ts != int(expires.timestamp())
        or request.expires_ts - request.created_ts != ttl_seconds
        or observed >= expires
    ):
        raise ValueError("Invalid pending transfer timing")


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid transfer queue timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Invalid transfer queue timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("Invalid transfer queue timestamp")
    return parsed


__all__ = ["TransferQueueReport"]
