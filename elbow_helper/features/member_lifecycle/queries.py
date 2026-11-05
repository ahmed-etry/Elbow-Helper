"""Bounded, read-only projections of member-lifecycle observations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


MAX_LIFECYCLE_MEMBERS = 10_000
MAX_RECRUITMENT_SOURCES = 100
# A lifecycle case and a record stay within this of each other name the same join.
SAME_JOIN_US = 1_000_000


@dataclass(frozen=True, slots=True)
class LifecycleActivityRegistration:
    member_id: int
    channel_id: int


@dataclass(frozen=True, slots=True)
class MemberLifecycleRow:
    member_id: int
    display_name: str
    joined_at: str
    platform: str
    overdue_applicant: bool
    last_seen_at: str | None
    last_seen_channel_id: int | None


@dataclass(frozen=True, slots=True)
class OverdueApplicant:
    member_id: int
    display_name: str


@dataclass(frozen=True, slots=True)
class MemberLifecycleSnapshot:
    observed_at: str
    stored_member_entry_count: int
    current_guild_member_count: int
    skipped_invalid_member_entries: int
    untracked_current_member_count: int
    skipped_invalid_platform_counts: int
    skipped_invalid_overdue_entries: int
    last_weekly_report_at: str | None
    last_applicant_scan_at: str | None
    platform_counts: tuple[tuple[str, int], ...]
    overdue_applicants: tuple[OverdueApplicant, ...]
    rows: tuple[MemberLifecycleRow, ...]


def _iso_us(at: int) -> str:
    return datetime.fromtimestamp(at / 1_000_000, timezone.utc).isoformat()


class LifecycleRecordSource:
    """Join and leave times from the family record's server membership, sources and
    reasons from lifecycle events, last messages from the Discord collector."""

    def __init__(self, lifecycle_state, people_reads: Callable[[], Any]) -> None:
        self._lifecycle = lifecycle_state
        self._people_reads = people_reads

    async def _stays(self, member_ids):
        reads = self._people_reads()
        if reads is None:
            raise ValueError("The family record is not available")
        rows, coverage = {}, None
        ordered = sorted(member_ids)
        for start in range(0, len(ordered), 200):
            batch = await reads.server_stays(ordered[start:start + 200])
            rows.update((row["member_id"], row) for row in batch["rows"])
            coverage = batch["coverage"]
        if coverage is None:
            coverage = (await reads.server_stays([]))["coverage"]
        return rows, coverage

    async def projection(self) -> dict[str, Any]:
        """The lifecycle read model: current stays of members whose join source is known."""
        state = self._lifecycle.snapshot()
        joins = self._lifecycle.open_joins()
        if len(joins) > MAX_LIFECYCLE_MEMBERS:
            raise ValueError("Invalid member lifecycle state")
        stays, _ = await self._stays(joins)
        members, last_seen = {}, {}
        for member_id, join in joins.items():
            row = stays.get(member_id, {"stays": [], "last_message": None})
            current = [stay for stay in row["stays"] if stay["to_at"] is None]
            if not current:
                continue
            members[str(member_id)] = {
                "joined_at_iso": _iso_us(current[-1]["discord_joined_at"] or current[-1]["from_at"]),
                "platform": join["platform"], "left": False,
            }
            message = row["last_message"]
            if message is not None:
                last_seen[str(member_id)] = {"ts_iso": _iso_us(message["message_at"]),
                                             "channel_id": message["channel_id"]}
        return {**state, "members": members, "last_seen": last_seen}

    async def history(self, member_id: int) -> dict[str, Any]:
        """Every recorded stay of one member, oldest first, with its source and leave reason."""
        events = await self._lifecycle.events.read(workflow="lifecycle", subject=member_id)
        stays, coverage = await self._stays([member_id])
        cases: dict[int, dict[str, Any]] = {}
        for event in events:
            if event["kind"] in ("joined", "left"):
                cases.setdefault(int(event["case_key"].split(":")[1]), {})[event["kind"]] = event
        entries, matched = [], set()

        def entry(kind, at, basis, event):
            detail = event["detail"] if event else {}
            return {"event": kind, "at": at, "basis": basis,
                    "platform": detail.get("platform") if kind == "join" else None,
                    "reason": detail.get("reason") if kind == "leave" else None,
                    "message_id": event["source_message_id"] if event else None}

        for stay in stays.get(member_id, {"stays": []})["stays"]:
            # Discord reports one join time with different sub-second precision on different paths.
            key = next((joined_at for joined_at in cases if stay["discord_joined_at"] is not None
                        and joined_at not in matched
                        and abs(joined_at - stay["discord_joined_at"]) < SAME_JOIN_US), None)
            case = cases.get(key, {})
            if case:
                matched.add(key)
            entries.append(entry("join", stay["discord_joined_at"] or stay["from_at"], stay["start_basis"],
                                 case.get("joined")))
            if stay["to_at"] is not None:
                entries.append(entry("leave", stay["to_at"], stay["end_basis"], case.get("left")))
        # A stay the record never observed: its case names Discord's join time.
        for joined_at, case in cases.items():
            if joined_at in matched:
                continue
            if "joined" in case:
                entries.append(entry("join", joined_at, "lifecycle_join", case["joined"]))
            if "left" in case:
                entries.append(entry("leave", case["left"]["occurred_at"], "lifecycle_leave", case["left"]))
        entries.sort(key=lambda item: (item["at"], item["event"] == "leave"))
        signal = coverage["signals"][0] if coverage["signals"] else {}
        return {"entries": entries, "recorded_since": signal.get("collection_start"),
                "complete": bool(coverage.get("complete"))}


class MemberLifecycleQueries:
    """Read lifecycle state without exposing its listeners or controls."""

    def __init__(
        self,
        state_source: Callable[[], Mapping[str, Any]],
        *,
        clock: Callable[[], datetime] | None = None,
        source: LifecycleRecordSource | None = None,
    ) -> None:
        self._state_source = state_source
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._source = source

    async def current(self) -> "MemberLifecycleQueries":
        """Queries over one consistent read of state, events and the family record."""
        if self._source is None:
            return self
        projection = await self._source.projection()
        return MemberLifecycleQueries(lambda: projection, clock=self._clock)

    async def history(self, member_id: int) -> dict[str, Any]:
        if self._source is None:
            raise ValueError("Member lifecycle history is not available")
        return await self._source.history(member_id)

    def activity_registrations(
        self, *, current_member_ids: set[int] | frozenset[int],
    ) -> tuple[LifecycleActivityRegistration, ...]:
        current = _member_ids(current_member_ids)
        state = self._load()
        last_seen = state.get("last_seen", {})
        if not isinstance(last_seen, Mapping) or len(last_seen) > MAX_LIFECYCLE_MEMBERS:
            raise ValueError("Invalid member activity state")
        registrations = []
        for raw_member_id, value in last_seen.items():
            member_id = _positive_int(raw_member_id)
            if member_id not in current or not isinstance(value, Mapping):
                continue
            channel_id = _positive_int(value.get("channel_id"))
            if channel_id is not None:
                registrations.append(LifecycleActivityRegistration(
                    member_id, channel_id,
                ))
        registrations.sort(key=lambda item: item.member_id)
        return tuple(registrations)

    def snapshot(
        self,
        *,
        current_members: Mapping[int, str],
        activity_channels: Mapping[int, int],
    ) -> MemberLifecycleSnapshot:
        current = _members(current_members)
        authorized_activity = _activity_channels(activity_channels, current)
        state = self._load()
        members = state.get("members", {})
        if not isinstance(members, Mapping) or len(members) > MAX_LIFECYCLE_MEMBERS:
            raise ValueError("Invalid member lifecycle state")
        overdue, skipped_overdue = _overdue_members(
            state.get("overdue_applicant_ids", ()), current,
        )
        overdue_ids = {item.member_id for item in overdue}
        last_seen = state.get("last_seen", {})
        if not isinstance(last_seen, Mapping) or len(last_seen) > MAX_LIFECYCLE_MEMBERS:
            raise ValueError("Invalid member activity state")

        rows = []
        skipped_members = 0
        represented = set()
        for raw_member_id, value in members.items():
            member_id = _positive_int(raw_member_id)
            if member_id is None or not isinstance(value, Mapping):
                skipped_members += 1
                continue
            joined_at = _timestamp(value.get("joined_at_iso"))
            platform = value.get("platform")
            left = value.get("left", False)
            if (
                joined_at is None
                or not isinstance(platform, str) or not platform or len(platform) > 100
                or type(left) is not bool
            ):
                skipped_members += 1
                continue
            if member_id not in current or left:
                continue
            activity_at = None
            activity_channel_id = None
            activity = last_seen.get(str(member_id), last_seen.get(member_id))
            if isinstance(activity, Mapping):
                channel_id = _positive_int(activity.get("channel_id"))
                observed_at = _timestamp(activity.get("ts_iso"))
                if (
                    channel_id is not None and observed_at is not None
                    and authorized_activity.get(member_id) == channel_id
                ):
                    activity_at = observed_at
                    activity_channel_id = channel_id
            represented.add(member_id)
            rows.append(MemberLifecycleRow(
                member_id=member_id,
                display_name=current[member_id],
                joined_at=joined_at,
                platform=platform,
                overdue_applicant=member_id in overdue_ids,
                last_seen_at=activity_at,
                last_seen_channel_id=activity_channel_id,
            ))
        rows.sort(key=lambda item: (item.joined_at, item.member_id), reverse=True)
        platform_counts, skipped_platforms = _platform_counts(
            state.get("platform_counts", {}),
        )
        observed = self._clock()
        if not isinstance(observed, datetime):
            raise ValueError("Invalid lifecycle observation time")
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        observed = observed.astimezone(timezone.utc)
        return MemberLifecycleSnapshot(
            observed_at=observed.isoformat(),
            stored_member_entry_count=len(members),
            current_guild_member_count=len(current),
            skipped_invalid_member_entries=skipped_members,
            untracked_current_member_count=len(current) - len(represented),
            skipped_invalid_platform_counts=skipped_platforms,
            skipped_invalid_overdue_entries=skipped_overdue,
            last_weekly_report_at=_optional_timestamp(
                state.get("last_weekly_report_iso"),
            ),
            last_applicant_scan_at=_optional_timestamp(
                state.get("last_applicant_scan_iso"),
            ),
            platform_counts=platform_counts,
            overdue_applicants=overdue,
            rows=tuple(rows),
        )

    def _load(self) -> Mapping[str, Any]:
        state = self._state_source()
        if not isinstance(state, Mapping):
            raise ValueError("Invalid member lifecycle state")
        return state


def _members(values: Mapping[int, str]) -> dict[int, str]:
    if not isinstance(values, Mapping) or len(values) > MAX_LIFECYCLE_MEMBERS:
        raise ValueError("Invalid current member selection")
    result = {}
    for member_id, display_name in values.items():
        if (
            type(member_id) is not int or member_id <= 0
            or not isinstance(display_name, str) or not display_name
            or len(display_name) > 100
        ):
            raise ValueError("Invalid current member selection")
        result[member_id] = display_name
    return result


def _member_ids(values: set[int] | frozenset[int]) -> frozenset[int]:
    if (
        not isinstance(values, (set, frozenset))
        or len(values) > MAX_LIFECYCLE_MEMBERS
        or any(type(value) is not int or value <= 0 for value in values)
    ):
        raise ValueError("Invalid current member selection")
    return frozenset(values)


def _activity_channels(
    values: Mapping[int, int], current: Mapping[int, str],
) -> dict[int, int]:
    if not isinstance(values, Mapping) or len(values) > MAX_LIFECYCLE_MEMBERS:
        raise ValueError("Invalid authorized activity selection")
    result = {}
    for member_id, channel_id in values.items():
        if (
            type(member_id) is not int or member_id not in current
            or type(channel_id) is not int or channel_id <= 0
        ):
            raise ValueError("Invalid authorized activity selection")
        result[member_id] = channel_id
    return result


def _overdue_members(
    values: Any, current: Mapping[int, str],
) -> tuple[tuple[OverdueApplicant, ...], int]:
    if not isinstance(values, (list, tuple)) or len(values) > MAX_LIFECYCLE_MEMBERS:
        raise ValueError("Invalid overdue applicant state")
    result = {}
    skipped = 0
    for value in values:
        member_id = _positive_int(value)
        if member_id is None:
            skipped += 1
        elif member_id in current:
            result[member_id] = OverdueApplicant(member_id, current[member_id])
    return tuple(result[key] for key in sorted(result)), skipped


def _platform_counts(value: Any) -> tuple[tuple[tuple[str, int], ...], int]:
    if not isinstance(value, Mapping) or len(value) > MAX_RECRUITMENT_SOURCES:
        raise ValueError("Invalid recruitment source counters")
    rows = []
    skipped = 0
    for platform, count in value.items():
        if (
            not isinstance(platform, str) or not platform or len(platform) > 100
            or type(count) is not int or count < 0
        ):
            skipped += 1
            continue
        rows.append((platform, count))
    rows.sort(key=lambda item: (-item[1], item[0].casefold()))
    return tuple(rows), skipped


def _positive_int(value: Any) -> int | None:
    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _optional_timestamp(value: Any) -> str | None:
    return None if value is None else _timestamp(value)


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc).isoformat()
    except (OverflowError, ValueError):
        return None


__all__ = [
    "LifecycleActivityRegistration", "LifecycleRecordSource", "MAX_LIFECYCLE_MEMBERS",
    "MemberLifecycleQueries", "MemberLifecycleRow", "MemberLifecycleSnapshot",
    "OverdueApplicant",
]
