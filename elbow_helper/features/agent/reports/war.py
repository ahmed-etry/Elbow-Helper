"""Retained regular-war evidence from the Wars feature."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import math
from typing import Any

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.features.wars.queries import RegularWarSnapshot


@dataclass(frozen=True, slots=True)
class RegularWarReport:
    report_id: str
    guild_id: int
    snapshot: RegularWarSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.report_id, str) or not self.report_id
            or len(self.report_id) > 32 or type(self.guild_id) is not int
            or self.guild_id <= 0 or self.snapshot.clan_code not in CLANS
            or self.snapshot.selected not in {"current", "previous"}
            or not self.snapshot.war_id
            or len(self.snapshot.war_id) > 300
            or self.snapshot.state not in {"preparation", "inwar", "warended"}
            or self.snapshot.evidence_status not in {
                "observed", "cached_unverified", "observation_mismatch", "stored_previous",
            }
        ):
            raise ValueError("Invalid regular-war report identity")
        if self.snapshot.evidence_status in {"observed", "observation_mismatch"}:
            _timestamp(self.snapshot.observed_at)
        elif self.snapshot.observed_at is not None:
            raise ValueError("Unverified regular-war evidence has an observation time")
        for value in (
            self.snapshot.preparation_start_at, self.snapshot.start_at,
            self.snapshot.end_at,
        ):
            if value is not None:
                _timestamp(value)
        if (
            (self.snapshot.selected == "previous")
            != (self.snapshot.evidence_status == "stored_previous")
        ):
            raise ValueError("Invalid regular-war report selection")
        if (
            type(self.snapshot.team_size) is not int
            or not 1 <= self.snapshot.team_size <= 50
            or type(self.snapshot.attacks_per_member) is not int
            or not 1 <= self.snapshot.attacks_per_member <= 2
            or type(self.snapshot.roster_complete) is not bool
            or self.snapshot.roster_complete != (
                len(self.snapshot.members) == self.snapshot.team_size
            )
        ):
            raise ValueError("Invalid regular-war report coverage")
        tags = [member.player_tag for member in self.snapshot.members]
        if len(tags) != len(set(tags)):
            raise ValueError("Duplicate account in regular-war report")
        for member in self.snapshot.members:
            if (
                normalize_player_tag(member.player_tag) != member.player_tag
                or not isinstance(member.player_name, str) or not member.player_name
                or len(member.player_name) > 100
                or any(type(value) is not int or value < 0 for value in (
                    member.townhall, member.map_position, member.attacks_expected,
                    member.attacks_used, member.attacks_remaining,
                    member.missed_attacks, member.stars,
                ))
                or member.attacks_expected != self.snapshot.attacks_per_member
                or not 0 <= member.townhall <= 20
                or not 0 <= member.map_position <= 50
                or member.attacks_used + member.attacks_remaining != member.attacks_expected
                or member.missed_attacks != (
                    member.attacks_remaining if self.snapshot.state == "warended" else 0
                )
                or not math.isfinite(member.destruction) or member.destruction < 0
                or member.stars > 3 * member.attacks_expected
                or member.destruction > 100 * member.attacks_expected
            ):
                raise ValueError("Invalid regular-war member evidence")
        expected_issues = (
            () if self.snapshot.roster_complete
            else (f"incomplete_roster:{len(self.snapshot.members)}/{self.snapshot.team_size}",)
        )
        if self.snapshot.issues != expected_issues or len(self.snapshot.issues) > 100 or any(
            not isinstance(issue, str) or not issue or len(issue) > 200
            for issue in self.snapshot.issues
        ):
            raise ValueError("Invalid regular-war report issues")
        if self.snapshot.result not in {None, "won", "lost", "tied"}:
            raise ValueError("Invalid regular-war result")
        for text in (
            self.snapshot.clan_tag, self.snapshot.clan_name,
            self.snapshot.opponent_tag, self.snapshot.opponent_name,
        ):
            if text is not None and (not isinstance(text, str) or not text or len(text) > 100):
                raise ValueError("Invalid regular-war side identity")
        for score in (self.snapshot.clan_stars, self.snapshot.opponent_stars):
            if score is not None and (type(score) is not int or score < 0):
                raise ValueError("Invalid regular-war score")
        for destruction in (
            self.snapshot.clan_destruction, self.snapshot.opponent_destruction,
        ):
            if destruction is not None and (
                isinstance(destruction, bool) or not isinstance(destruction, (int, float))
                or not math.isfinite(destruction) or not 0 <= destruction <= 100
            ):
                raise ValueError("Invalid regular-war score")
        object.__setattr__(self, "retained_bytes", len(json.dumps(
            self.storage_payload(), ensure_ascii=False,
        ).encode("utf-8")))

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "guild_id": self.guild_id,
            "snapshot": asdict(self.snapshot),
        }

    def manifest(self) -> dict[str, Any]:
        members = self.snapshot.members
        return {
            "report_id": self.report_id, "kind": "regular_war",
            "clan_code": self.snapshot.clan_code,
            "evidence_status": self.snapshot.evidence_status,
            "observed_at": self.snapshot.observed_at,
            "selected": self.snapshot.selected, "war_id": self.snapshot.war_id,
            "state": self.snapshot.state,
            "preparation_start_at": self.snapshot.preparation_start_at,
            "start_at": self.snapshot.start_at, "end_at": self.snapshot.end_at,
            "team_size": self.snapshot.team_size,
            "attacks_per_member": self.snapshot.attacks_per_member,
            "clan": {
                "tag": self.snapshot.clan_tag, "name": self.snapshot.clan_name,
                "stars": self.snapshot.clan_stars,
                "destruction": self.snapshot.clan_destruction,
            },
            "opponent": {
                "tag": self.snapshot.opponent_tag, "name": self.snapshot.opponent_name,
                "stars": self.snapshot.opponent_stars,
                "destruction": self.snapshot.opponent_destruction,
            },
            "result": self.snapshot.result,
            "roster_complete": self.snapshot.roster_complete,
            "total_members": len(members),
            "attack_totals": {
                "expected": sum(row.attacks_expected for row in members),
                "used": sum(row.attacks_used for row in members),
                "remaining": sum(row.attacks_remaining for row in members),
                "missed": sum(row.missed_attacks for row in members),
                "players_with_missed_attacks": sum(row.missed_attacks > 0 for row in members),
            },
            "issues": list(self.snapshot.issues), "complete_snapshot": True,
        }

    def page(self, *, offset: int = 0, limit: int = 25) -> dict[str, Any]:
        return {
            **self.manifest(),
            "members": [asdict(row) for row in self.snapshot.members[offset:offset + limit]],
            "next_offset": (
                offset + limit if offset + limit < len(self.snapshot.members) else None
            ),
        }


def _timestamp(value: str | None) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid regular-war observation time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Invalid regular-war observation time") from error
    if parsed.tzinfo is None:
        raise ValueError("Invalid regular-war observation time")


__all__ = ["RegularWarReport"]
