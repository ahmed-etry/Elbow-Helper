"""Retained historical regular-war evidence with current ownership joins."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import math
from typing import Any

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.features.clan_health.queries import (
    HistoricalRegularWarHistory, HistoricalRegularWarMember,
)


@dataclass(frozen=True, slots=True)
class HistoricalWarOwnedMember:
    source: HistoricalRegularWarMember
    linked_member_id: int | None
    linked_player_name: str | None


@dataclass(frozen=True, slots=True)
class HistoricalWarMemberSummary:
    linked_member_id: int | None
    ownership_status: str
    player_tags: tuple[str, ...]
    war_entries: int
    wars_participated: int
    attacks_expected: int
    attacks_used: int
    attacks_missed: int
    wars_with_misses: int
    incomplete_attack_detail_entries: int


@dataclass(frozen=True, slots=True)
class HistoricalRegularWarReport:
    report_id: str
    guild_id: int
    ownership_observed_at: str
    history: HistoricalRegularWarHistory
    rows: tuple[HistoricalWarOwnedMember, ...]
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.report_id, str) or not self.report_id
            or len(self.report_id) > 32 or type(self.guild_id) is not int
            or self.guild_id <= 0 or self.history.clan_code not in CLANS
        ):
            raise ValueError("Invalid historical regular-war report identity")
        _timestamp(self.ownership_observed_at)
        _timestamp(self.history.read_at)
        source_keys = [(row.war_id, row.player_tag) for row in self.history.members]
        row_keys = [(row.source.war_id, row.source.player_tag) for row in self.rows]
        if (
            len(source_keys) != len(set(source_keys))
            or len(row_keys) != len(set(row_keys))
            or source_keys != row_keys
        ):
            raise ValueError("Historical regular-war rows do not match their source")
        war_ids = [war.war_id for war in self.history.wars]
        if (
            len(war_ids) != len(set(war_ids))
            or any(war.clan_code != self.history.clan_code for war in self.history.wars)
            or not set(key[0] for key in source_keys) <= set(war_ids)
            or self.history.next_before_war_id is not None and (
                not war_ids or self.history.next_before_war_id != war_ids[-1]
            )
        ):
            raise ValueError("Invalid historical regular-war coverage")
        if any(
            self.history.wars[index].end_ts < self.history.wars[index + 1].end_ts
            for index in range(len(self.history.wars) - 1)
        ):
            raise ValueError("Historical regular wars are not ordered newest first")
        members_by_war: dict[str, list[HistoricalRegularWarMember]] = {}
        for member in self.history.members:
            members_by_war.setdefault(member.war_id, []).append(member)
        for war in self.history.wars:
            _validate_war(war, members_by_war.get(war.war_id, []))
        ownership_by_tag: dict[str, tuple[int | None, str | None]] = {}
        for row in self.rows:
            _validate_owned_row(row)
            ownership = (row.linked_member_id, row.linked_player_name)
            previous = ownership_by_tag.setdefault(row.source.player_tag, ownership)
            if previous != ownership:
                raise ValueError("Inconsistent current ownership in historical war report")
        object.__setattr__(self, "retained_bytes", len(json.dumps(
            self.storage_payload(), ensure_ascii=False,
        ).encode("utf-8")))

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "guild_id": self.guild_id,
            "ownership_observed_at": self.ownership_observed_at,
            "history": asdict(self.history), "rows": [asdict(row) for row in self.rows],
        }

    def member_summaries(self) -> tuple[HistoricalWarMemberSummary, ...]:
        groups: dict[tuple[str, int | str], list[HistoricalWarOwnedMember]] = {}
        for row in self.rows:
            key: tuple[str, int | str] = (
                ("member", row.linked_member_id)
                if row.linked_member_id is not None
                else ("account", row.source.player_tag)
            )
            groups.setdefault(key, []).append(row)
        summaries = []
        for (kind, identity), rows in groups.items():
            summaries.append(HistoricalWarMemberSummary(
                linked_member_id=identity if kind == "member" else None,
                ownership_status="linked_currently" if kind == "member" else "unlinked_currently",
                player_tags=tuple(sorted({row.source.player_tag for row in rows})),
                war_entries=len(rows),
                wars_participated=len({row.source.war_id for row in rows}),
                attacks_expected=sum(row.source.attacks_expected for row in rows),
                attacks_used=sum(row.source.attacks_used for row in rows),
                attacks_missed=sum(row.source.attacks_missed for row in rows),
                wars_with_misses=len({
                    row.source.war_id for row in rows if row.source.attacks_missed > 0
                }),
                incomplete_attack_detail_entries=sum(
                    not row.source.attack_details_complete for row in rows
                ),
            ))
        summaries.sort(key=lambda row: (
            -row.attacks_missed, -row.wars_with_misses,
            row.linked_member_id is None,
            row.linked_member_id or 0, row.player_tags,
        ))
        return tuple(summaries)

    def manifest(self) -> dict[str, Any]:
        summaries = self.member_summaries()
        return {
            "report_id": self.report_id, "kind": "historical_regular_wars",
            "clan_code": self.history.clan_code,
            "history_read_at": self.history.read_at,
            "ownership_observed_at": self.ownership_observed_at,
            "wars": [asdict(war) for war in self.history.wars],
            "war_count": len(self.history.wars),
            "war_entry_count": len(self.rows),
            "unique_accounts": len({row.source.player_tag for row in self.rows}),
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
            "attacks_expected": sum(row.source.attacks_expected for row in self.rows),
            "attacks_used": sum(row.source.attacks_used for row in self.rows),
            "attacks_missed": sum(row.source.attacks_missed for row in self.rows),
            "ownership_groups_with_misses": sum(
                row.attacks_missed > 0 for row in summaries
            ),
            "incomplete_wars": sum(
                not war.roster_complete or not war.attack_details_complete
                for war in self.history.wars
            ),
            "next_before_war_id": self.history.next_before_war_id,
            "complete_selected_history": True,
            "ownership_interpretation": (
                "Linked member IDs are current as of ownership_observed_at; they do not prove "
                "who controlled an account when an older war occurred. Unlinked accounts still "
                "retain their gameplay evidence."
            ),
        }

    def page(
        self, *, view: str = "member_summaries", offset: int = 0, limit: int = 25,
        player_tag: str | None = None, member_id: int | None = None,
    ) -> dict[str, Any]:
        if view == "member_summaries":
            values: tuple[Any, ...] = self.member_summaries()
            key = "member_summaries"
        elif view == "war_rows":
            values = self.rows
            if player_tag is not None:
                values = tuple(row for row in values if row.source.player_tag == player_tag)
            if member_id is not None:
                values = tuple(row for row in values if row.linked_member_id == member_id)
            key = "war_rows"
        else:
            raise ValueError("Unknown historical regular-war report view")
        return {
            **self.manifest(), "view": view, "matched_count": len(values),
            key: [asdict(row) for row in values[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(values) else None,
        }


def _validate_owned_row(row: HistoricalWarOwnedMember) -> None:
    source = row.source
    if (
        not source.war_id or len(source.war_id) > 300
        or normalize_player_tag(source.player_tag) != source.player_tag
        or not isinstance(source.player_name, str) or not source.player_name
        or len(source.player_name) > 100
        or any(type(value) is not int or value < 0 for value in (
            source.end_ts, source.townhall, source.map_position,
            source.attacks_expected, source.attacks_used, source.attacks_missed,
            source.attack_details_recorded, source.stars, source.fresh_attacks,
        ))
        or source.attacks_used + source.attacks_missed != source.attacks_expected
        or source.attacks_expected > 2
        or source.townhall > 20 or source.map_position > 50
        or source.attack_details_complete != (
            source.attack_details_recorded == source.attacks_used
        )
        or not math.isfinite(source.destruction) or source.destruction < 0
        or source.stars > 3 * source.attack_details_recorded
        or source.destruction > 100 * source.attack_details_recorded
        or source.fresh_attacks > source.attack_details_recorded
        or row.linked_member_id is not None and (
            type(row.linked_member_id) is not int or row.linked_member_id <= 0
        )
        or row.linked_player_name is not None and (
            not isinstance(row.linked_player_name, str)
            or not row.linked_player_name or len(row.linked_player_name) > 100
        )
    ):
        raise ValueError("Invalid historical regular-war member evidence")


def _validate_war(war: Any, members: list[HistoricalRegularWarMember]) -> None:
    if (
        not isinstance(war.war_id, str) or not war.war_id or len(war.war_id) > 300
        or war.clan_code not in CLANS
        or not isinstance(war.clan_tag, str) or not war.clan_tag or len(war.clan_tag) > 20
        or war.opponent_tag is not None and (
            not isinstance(war.opponent_tag, str) or not war.opponent_tag
            or len(war.opponent_tag) > 20
        )
        or war.opponent_name is not None and (
            not isinstance(war.opponent_name, str) or not war.opponent_name
            or len(war.opponent_name) > 100
        )
        or any(type(value) is not int or value < 0 for value in (
            war.preparation_start_ts, war.start_ts, war.end_ts,
            war.last_seen_ts, war.roster_entries, war.attack_rows,
        ))
        or type(war.team_size) is not int or not 1 <= war.team_size <= 50
        or type(war.attacks_per_member) is not int or not 1 <= war.attacks_per_member <= 2
        or type(war.roster_complete) is not bool
        or type(war.attack_details_complete) is not bool
        or war.roster_entries != len(members)
        or war.roster_complete != (len(members) == war.team_size)
        or any(member.attacks_expected != war.attacks_per_member for member in members)
    ):
        raise ValueError("Invalid historical regular-war identity or coverage")
    detail_rows = sum(member.attack_details_recorded for member in members)
    orphan_rows = war.attack_rows - detail_rows
    if orphan_rows < 0:
        raise ValueError("Invalid historical regular-war attack coverage")
    expected_issues = []
    if not war.roster_complete:
        expected_issues.append(f"incomplete_roster:{len(members)}/{war.team_size}")
    for member in members:
        if not member.attack_details_complete:
            expected_issues.append(
                f"attack_detail_mismatch:{member.player_tag}:"
                f"{member.attacks_used}/{member.attack_details_recorded}"
            )
    if orphan_rows:
        expected_issues.append(f"orphan_attack_details:{orphan_rows}")
    if war.issues != tuple(expected_issues):
        raise ValueError("Historical regular-war issues do not match coverage")
    if war.attack_details_complete != (
        not orphan_rows and all(member.attack_details_complete for member in members)
    ):
        raise ValueError("Invalid historical regular-war detail coverage")


def _timestamp(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid historical regular-war timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Invalid historical regular-war timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("Invalid historical regular-war timestamp")


__all__ = [
    "HistoricalRegularWarReport", "HistoricalWarMemberSummary",
    "HistoricalWarOwnedMember",
]
