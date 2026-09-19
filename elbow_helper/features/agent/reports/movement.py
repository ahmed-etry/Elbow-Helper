"""Retained family-roster movement evidence with current ownership context."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from typing import Any

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.features.clan_health.queries import (
    FamilyAccountMovement,
    FamilyMovementHistory,
)


TRANSITIONS = {
    "observed_entered_family",
    "observed_left_family",
    "observed_family_clan_change",
}


@dataclass(frozen=True, slots=True)
class OwnedFamilyAccountMovement:
    source: FamilyAccountMovement
    linked_member_id: int | None
    linked_player_name: str | None


@dataclass(frozen=True, slots=True)
class FamilyMovementOwnerSummary:
    linked_member_id: int | None
    ownership_status: str
    player_tags: tuple[str, ...]
    movement_count: int
    observed_family_clan_changes: int
    observed_family_entries: int
    observed_family_exits: int
    first_interval_start_ts: int
    last_interval_end_ts: int


@dataclass(frozen=True, slots=True)
class FamilyMovementReport:
    report_id: str
    guild_id: int
    ownership_observed_at: str
    history: FamilyMovementHistory
    rows: tuple[OwnedFamilyAccountMovement, ...]
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.report_id, str) or not self.report_id
            or len(self.report_id) > 32 or type(self.guild_id) is not int
            or self.guild_id <= 0
        ):
            raise ValueError("Invalid family-movement report identity")
        _timestamp(self.ownership_observed_at)
        _timestamp(self.history.read_at)
        _validate_history(self.history)
        source_keys = [_movement_key(row) for row in self.history.movements]
        row_keys = [_movement_key(row.source) for row in self.rows]
        if source_keys != row_keys or len(row_keys) != len(set(row_keys)):
            raise ValueError("Family-movement ownership rows do not match their source")
        ownership_by_tag: dict[str, tuple[int | None, str | None]] = {}
        for row in self.rows:
            if (
                row.linked_member_id is not None and (
                    type(row.linked_member_id) is not int or row.linked_member_id <= 0
                )
                or row.linked_player_name is not None and (
                    not isinstance(row.linked_player_name, str)
                    or not row.linked_player_name or len(row.linked_player_name) > 100
                )
            ):
                raise ValueError("Invalid current ownership in family-movement report")
            ownership = (row.linked_member_id, row.linked_player_name)
            previous = ownership_by_tag.setdefault(row.source.player_tag, ownership)
            if previous != ownership:
                raise ValueError("Inconsistent current ownership in family-movement report")
        object.__setattr__(self, "retained_bytes", len(json.dumps(
            self.storage_payload(), ensure_ascii=False,
        ).encode("utf-8")))

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "guild_id": self.guild_id,
            "ownership_observed_at": self.ownership_observed_at,
            "history": asdict(self.history),
            "rows": [asdict(row) for row in self.rows],
        }

    def owner_summaries(self) -> tuple[FamilyMovementOwnerSummary, ...]:
        groups: dict[tuple[str, int | str], list[OwnedFamilyAccountMovement]] = {}
        for row in self.rows:
            key: tuple[str, int | str] = (
                ("member", row.linked_member_id)
                if row.linked_member_id is not None
                else ("account", row.source.player_tag)
            )
            groups.setdefault(key, []).append(row)
        summaries = []
        for (kind, identity), rows in groups.items():
            counts = Counter(row.source.transition for row in rows)
            summaries.append(FamilyMovementOwnerSummary(
                linked_member_id=identity if kind == "member" else None,
                ownership_status=(
                    "linked_currently" if kind == "member" else "unlinked_currently"
                ),
                player_tags=tuple(sorted({row.source.player_tag for row in rows})),
                movement_count=len(rows),
                observed_family_clan_changes=counts["observed_family_clan_change"],
                observed_family_entries=counts["observed_entered_family"],
                observed_family_exits=counts["observed_left_family"],
                first_interval_start_ts=min(row.source.older_observed_ts for row in rows),
                last_interval_end_ts=max(row.source.newer_observed_ts for row in rows),
            ))
        summaries.sort(key=lambda row: (
            -row.movement_count, row.linked_member_id is None,
            row.linked_member_id or 0, row.player_tags,
        ))
        return tuple(summaries)

    def manifest(self) -> dict[str, Any]:
        base = movement_history_manifest(self.history)
        transition_counts = Counter(row.source.transition for row in self.rows)
        pair_counts = Counter(
            f"{row.source.from_clan_code or 'outside_family'} -> "
            f"{row.source.to_clan_code or 'outside_family'}"
            for row in self.rows
        )
        return {
            **base, "report_id": self.report_id, "kind": "family_account_movements",
            "ownership_observed_at": self.ownership_observed_at,
            "transition_counts": dict(sorted(transition_counts.items())),
            "clan_observation_pairs": dict(sorted(pair_counts.items())),
            "currently_linked_accounts": len({
                row.source.player_tag for row in self.rows
                if row.linked_member_id is not None
            }),
            "currently_unlinked_accounts": len({
                row.source.player_tag for row in self.rows
                if row.linked_member_id is None
            }),
            "current_linked_members": len({
                row.linked_member_id for row in self.rows
                if row.linked_member_id is not None
            }),
            "current_ownership_groups_with_movements": len(self.owner_summaries()),
            "ownership_interpretation": (
                "Linked member IDs are current as of ownership_observed_at and do not "
                "prove historical ownership. Snapshot changes show observed family-roster "
                "presence, not transfer intent, destination outside the family, or an exact "
                "movement time."
            ),
        }

    def page(
        self, *, view: str = "owner_summaries", offset: int = 0, limit: int = 25,
        player_tag: str | None = None, member_id: int | None = None,
        transition: str | None = None, clan_code: str | None = None,
    ) -> dict[str, Any]:
        if view == "owner_summaries":
            values: tuple[Any, ...] = self.owner_summaries()
            key = "owner_summaries"
        elif view == "movements":
            values = self.rows
            if player_tag is not None:
                values = tuple(row for row in values if row.source.player_tag == player_tag)
            if member_id is not None:
                values = tuple(row for row in values if row.linked_member_id == member_id)
            if transition is not None:
                values = tuple(row for row in values if row.source.transition == transition)
            if clan_code is not None:
                values = tuple(row for row in values if clan_code in {
                    row.source.from_clan_code, row.source.to_clan_code,
                })
            key = "movements"
        else:
            raise ValueError("Unknown family-movement report view")
        return {
            **self.manifest(), "view": view, "matched_count": len(values),
            key: [asdict(row) for row in values[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(values) else None,
        }


def movement_history_manifest(history: FamilyMovementHistory) -> dict[str, Any]:
    ambiguous = {
        tag for interval in history.intervals
        for tag in interval.excluded_ambiguous_account_tags
    }
    return {
        "report_id": None, "kind": "family_account_movements",
        "history_read_at": history.read_at,
        "runs": [asdict(run) for run in history.runs],
        "intervals": [asdict(interval) for interval in history.intervals],
        "run_count": len(history.runs), "interval_count": len(history.intervals),
        "movement_count": len(history.movements),
        "excluded_ambiguous_accounts": len(ambiguous),
        "next_before_run_id": history.next_before_run_id,
        "complete_family_runs_only": True,
        "all_selected_intervals_evaluated": True,
        "movement_interpretation": (
            "A movement is a difference between consecutive complete family-roster "
            "observations. outside_family means the account was not observed in any "
            "family clan in that snapshot; it does not identify an external destination, "
            "transfer intent, or an exact movement time."
        ),
    }


def _movement_key(row: FamilyAccountMovement) -> tuple[str, str, str]:
    return row.older_run_id, row.newer_run_id, row.player_tag


def _validate_history(history: FamilyMovementHistory) -> None:
    if len(history.runs) > 21 or len(history.intervals) > 20:
        raise ValueError("Family-movement history exceeds its bounds")
    run_ids = [run.run_id for run in history.runs]
    if len(run_ids) != len(set(run_ids)):
        raise ValueError("Duplicate complete family snapshot")
    for index, run in enumerate(history.runs):
        if (
            not isinstance(run.run_id, str) or not run.run_id or len(run.run_id) > 100
            or any(type(value) is not int or value < 0 for value in (
                run.observed_ts, run.row_count, run.unique_account_count,
            ))
            or run.observed_ts <= 0 or run.unique_account_count > run.row_count
            or len(run.ambiguous_account_tags) != len(set(run.ambiguous_account_tags))
            or tuple(sorted(run.ambiguous_account_tags)) != run.ambiguous_account_tags
            or any(normalize_player_tag(tag) != tag for tag in run.ambiguous_account_tags)
            or index and history.runs[index - 1].observed_ts < run.observed_ts
        ):
            raise ValueError("Invalid complete family-snapshot evidence")
    if len(history.intervals) != max(0, len(history.runs) - 1):
        raise ValueError("Invalid family-snapshot interval coverage")
    movements_by_interval: Counter[tuple[str, str]] = Counter(
        (row.older_run_id, row.newer_run_id) for row in history.movements
    )
    interval_keys = set()
    intervals_by_key = {}
    for index, interval in enumerate(history.intervals):
        newer, older = history.runs[index], history.runs[index + 1]
        key = (interval.older_run_id, interval.newer_run_id)
        interval_keys.add(key)
        intervals_by_key[key] = interval
        if (
            key != (older.run_id, newer.run_id)
            or interval.older_observed_ts != older.observed_ts
            or interval.newer_observed_ts != newer.observed_ts
            or interval.older_account_count != older.unique_account_count
            or interval.newer_account_count != newer.unique_account_count
            or type(interval.movement_count) is not int or interval.movement_count < 0
            or interval.movement_count != movements_by_interval[key]
            or tuple(sorted(set(interval.excluded_ambiguous_account_tags)))
            != interval.excluded_ambiguous_account_tags
            or set(interval.excluded_ambiguous_account_tags) != set(
                older.ambiguous_account_tags + newer.ambiguous_account_tags
            )
        ):
            raise ValueError("Invalid family-snapshot interval evidence")
    for movement in history.movements:
        interval = intervals_by_key.get((movement.older_run_id, movement.newer_run_id))
        if (
            normalize_player_tag(movement.player_tag) != movement.player_tag
            or not isinstance(movement.player_name, str) or not movement.player_name
            or len(movement.player_name) > 100
            or movement.transition not in TRANSITIONS
            or interval is None
            or type(movement.older_observed_ts) is not int
            or type(movement.newer_observed_ts) is not int
            or movement.older_observed_ts <= 0
            or movement.newer_observed_ts < movement.older_observed_ts
            or movement.older_observed_ts != interval.older_observed_ts
            or movement.newer_observed_ts != interval.newer_observed_ts
            or not _valid_transition_clans(movement)
        ):
            raise ValueError("Invalid family account-movement evidence")
    if len({_movement_key(row) for row in history.movements}) != len(history.movements):
        raise ValueError("Duplicate family account-movement evidence")
    if history.next_before_run_id is not None and (
        not history.runs or history.next_before_run_id != history.runs[-1].run_id
    ):
        raise ValueError("Invalid family-snapshot cursor")


def _valid_transition_clans(row: FamilyAccountMovement) -> bool:
    if row.from_clan_code is not None and (
        not isinstance(row.from_clan_code, str) or row.from_clan_code not in CLANS
    ):
        return False
    if row.to_clan_code is not None and (
        not isinstance(row.to_clan_code, str) or row.to_clan_code not in CLANS
    ):
        return False
    if row.transition == "observed_entered_family":
        return row.from_clan_code is None and row.to_clan_code in CLANS
    if row.transition == "observed_left_family":
        return row.from_clan_code in CLANS and row.to_clan_code is None
    return (
        row.from_clan_code in CLANS and row.to_clan_code in CLANS
        and row.from_clan_code != row.to_clan_code
    )


def _timestamp(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid family-movement timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Invalid family-movement timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("Invalid family-movement timestamp")


__all__ = [
    "FamilyMovementOwnerSummary", "FamilyMovementReport",
    "OwnedFamilyAccountMovement", "TRANSITIONS", "movement_history_manifest",
]
