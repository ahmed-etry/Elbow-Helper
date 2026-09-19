"""Typed, read-only snapshots of unresolved clan-transfer requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from .config import CLAN_TRANSFER_QUEUES, REQUEST_TTL_HOURS


MAX_STORED_TRANSFER_REQUESTS = 1000


@dataclass(frozen=True, slots=True)
class PendingTransferRequest:
    member_id: int
    created_at: str
    created_ts: int
    expires_at: str
    expires_ts: int


@dataclass(frozen=True, slots=True)
class TransferQueueSnapshot:
    clan_code: str
    thread_id: int
    stored_request_count: int
    expired_stored_count: int
    pending: tuple[PendingTransferRequest, ...]


@dataclass(frozen=True, slots=True)
class TransferQueueRegistration:
    clan_code: str
    thread_id: int


@dataclass(frozen=True, slots=True)
class PendingTransferSnapshot:
    observed_at: str
    request_ttl_hours: int
    queues: tuple[TransferQueueSnapshot, ...]


class ClanTransferQueries:
    """Stable owner facade over the queue's in-memory persisted state."""

    def __init__(
        self, state: Callable[[], Mapping[str, Any]],
        *, clock: Callable[[], datetime] | None = None,
    ):
        self._state = state
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def queue_registrations(self) -> tuple[TransferQueueRegistration, ...]:
        return tuple(TransferQueueRegistration(
            clan_code=clan_code, thread_id=int(config["thread_id"]),
        ) for clan_code, config in CLAN_TRANSFER_QUEUES.items())

    def pending_snapshot(self, *, clan_codes: tuple[str, ...]) -> PendingTransferSnapshot:
        if (
            not isinstance(clan_codes, tuple)
            or any(
                not isinstance(code, str) or code not in CLAN_TRANSFER_QUEUES
                for code in clan_codes
            )
            or len(clan_codes) != len(set(clan_codes))
        ):
            raise ValueError("Invalid transfer queue selection")
        observed = self._clock()
        if not isinstance(observed, datetime):
            raise ValueError("Invalid transfer snapshot clock")
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        observed = observed.astimezone(timezone.utc)
        state = self._state()
        if not isinstance(state, Mapping):
            raise ValueError("Invalid transfer queue state")
        clans = state.get("clans", {})
        if not isinstance(clans, Mapping):
            raise ValueError("Invalid transfer queue state")
        total_rows = 0
        queues = []
        expiry_delta = timedelta(hours=REQUEST_TTL_HOURS)
        for clan_code in clan_codes:
            config = CLAN_TRANSFER_QUEUES[clan_code]
            raw_queue = clans.get(clan_code, {})
            if not isinstance(raw_queue, Mapping):
                raise ValueError("Invalid transfer queue state")
            raw_pending = raw_queue.get("pending", [])
            if not isinstance(raw_pending, list):
                raise ValueError("Invalid transfer queue state")
            total_rows += len(raw_pending)
            if total_rows > MAX_STORED_TRANSFER_REQUESTS:
                raise ValueError("Transfer queue state exceeds its read bound")
            pending = []
            expired = 0
            seen_members = set()
            for raw in raw_pending:
                if not isinstance(raw, Mapping):
                    raise ValueError("Invalid transfer request")
                member_id = raw.get("user_id")
                if type(member_id) is not int or member_id <= 0 or member_id in seen_members:
                    raise ValueError("Invalid transfer request member")
                seen_members.add(member_id)
                created = _timestamp(raw.get("created_at"))
                expires = created + expiry_delta
                if observed >= expires:
                    expired += 1
                    continue
                pending.append(PendingTransferRequest(
                    member_id=member_id,
                    created_at=created.isoformat(),
                    created_ts=int(created.timestamp()),
                    expires_at=expires.isoformat(),
                    expires_ts=int(expires.timestamp()),
                ))
            pending.sort(key=lambda row: (row.created_ts, row.member_id))
            queues.append(TransferQueueSnapshot(
                clan_code=clan_code, thread_id=int(config["thread_id"]),
                stored_request_count=len(raw_pending),
                expired_stored_count=expired, pending=tuple(pending),
            ))
        return PendingTransferSnapshot(
            observed_at=observed.isoformat(),
            request_ttl_hours=REQUEST_TTL_HOURS, queues=tuple(queues),
        )


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid transfer request timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Invalid transfer request timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("Invalid transfer request timestamp")
    return parsed.astimezone(timezone.utc)


__all__ = [
    "ClanTransferQueries", "MAX_STORED_TRANSFER_REQUESTS",
    "PendingTransferRequest", "PendingTransferSnapshot", "TransferQueueRegistration",
    "TransferQueueSnapshot",
]
