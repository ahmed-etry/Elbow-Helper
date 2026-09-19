"""Status-only reads of active hibernation state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Callable, Mapping

from .state import FALLBACK_INFO_MESSAGE_KEY, FALLBACK_THREADS_KEY


MAX_ACTIVE_HIBERNATION_RECORDS = 1000
_DISCORD_TIMESTAMP = re.compile(r"^<t:(\d+):F>$")


@dataclass(frozen=True, slots=True)
class ActiveHibernationRecord:
    member_id: int
    started_at: str | None
    started_ts: int | None
    start_time_status: str


@dataclass(frozen=True, slots=True)
class ActiveHibernationSnapshot:
    observed_at: str
    stored_member_entry_count: int
    skipped_invalid_member_entries: int
    ignored_metadata_entries: int
    missing_start_time_count: int
    records: tuple[ActiveHibernationRecord, ...]


class HibernationQueries:
    """Owner facade that never exposes saved roles or ticket metadata."""

    def __init__(
        self, state: Callable[[], Mapping[str, Any]],
        *, clock: Callable[[], datetime] | None = None,
    ):
        self._state = state
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def active_snapshot(self) -> ActiveHibernationSnapshot:
        observed = self._clock()
        if not isinstance(observed, datetime):
            raise ValueError("Invalid hibernation snapshot clock")
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        observed = observed.astimezone(timezone.utc)
        state = self._state()
        if not isinstance(state, Mapping):
            raise ValueError("Invalid hibernation state")

        candidates = []
        ignored_metadata = 0
        invalid = 0
        for raw_member_id, value in state.items():
            if raw_member_id in {FALLBACK_THREADS_KEY, FALLBACK_INFO_MESSAGE_KEY} or (
                isinstance(raw_member_id, str) and raw_member_id.startswith("_")
            ):
                ignored_metadata += 1
                continue
            if not isinstance(raw_member_id, str) or not raw_member_id.isdigit():
                invalid += 1
                continue
            candidates.append((int(raw_member_id), value))
        if len(candidates) > MAX_ACTIVE_HIBERNATION_RECORDS:
            raise ValueError("Hibernation state exceeds its read bound")

        records = []
        missing_dates = 0
        seen_members = set()
        for member_id, value in candidates:
            if member_id <= 0 or member_id in seen_members or not isinstance(value, Mapping):
                invalid += 1
                continue
            seen_members.add(member_id)
            started_ts = _start_timestamp(value.get("hibernation_date"))
            if started_ts is None:
                missing_dates += 1
                started_at = None
                status = "unavailable"
            else:
                started_at = datetime.fromtimestamp(
                    started_ts, tz=timezone.utc,
                ).isoformat()
                status = "recorded"
            records.append(ActiveHibernationRecord(
                member_id=member_id, started_at=started_at,
                started_ts=started_ts, start_time_status=status,
            ))
        records.sort(key=lambda row: (
            row.started_ts is None,
            -(row.started_ts or 0), row.member_id,
        ))
        return ActiveHibernationSnapshot(
            observed_at=observed.isoformat(),
            stored_member_entry_count=len(records) + invalid,
            skipped_invalid_member_entries=invalid,
            ignored_metadata_entries=ignored_metadata,
            missing_start_time_count=missing_dates,
            records=tuple(records),
        )


def _start_timestamp(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = _DISCORD_TIMESTAMP.fullmatch(value)
    if match is None:
        return None
    timestamp = int(match.group(1))
    return timestamp if timestamp > 0 else None


__all__ = [
    "ActiveHibernationRecord", "ActiveHibernationSnapshot",
    "HibernationQueries", "MAX_ACTIVE_HIBERNATION_RECORDS",
]
