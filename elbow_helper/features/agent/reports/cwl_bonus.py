"""Restart-persistent configured CWL bonus-scoring evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
import re
from typing import Any

from elbow_helper.configuration.clans import CLAN_ORDER
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.features.cwl.queries import CwlBonusScopeSnapshot


@dataclass(frozen=True, slots=True)
class CwlBonusScopeReport:
    report_id: str
    guild_id: int
    snapshot: CwlBonusScopeSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        value = self.snapshot
        scope_valid = (
            value.scope_type == "season"
            and value.requested_round is None
            and value.requested_war_tag is None
            or value.scope_type == "round"
            and type(value.requested_round) is int
            and 1 <= value.requested_round <= 7
            and value.requested_war_tag is None
            or value.scope_type == "war"
            and value.requested_round is None
            and isinstance(value.requested_war_tag, str)
            and bool(value.requested_war_tag)
            and len(value.requested_war_tag) <= 100
        )
        if (
            not isinstance(self.report_id, str) or not self.report_id
            or len(self.report_id) > 32
            or type(self.guild_id) is not int or self.guild_id <= 0
            or not isinstance(value.observed_at, str) or not value.observed_at
            or value.clan_code not in CLAN_ORDER
            or re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", value.season) is None
            or not scope_valid
            or value.coverage_status not in {
                "stored_season_scoring", "selected_scored_attacks",
                "no_matching_scored_attacks",
            }
            or tuple(sorted(set(value.resolved_rounds)))
            != value.resolved_rounds
            or any(type(item) is not int or not 1 <= item <= 7
                   for item in value.resolved_rounds)
            or tuple(sorted(set(value.resolved_war_tags)))
            != value.resolved_war_tags
            or any(not isinstance(item, str) or not item or len(item) > 100
                   for item in value.resolved_war_tags)
            or len(value.summaries) > 500
            or len(value.ineligible) > 500
            or len(value.attacks) > 1_000
            or value.scope_type != "season"
            and (value.summaries or value.ineligible)
            or value.coverage_status == "no_matching_scored_attacks"
            and value.attacks
            or not _valid_settings(value.settings)
            or any(not _valid_summary(item) for item in value.summaries)
            or any(not _valid_ineligible(item) for item in value.ineligible)
            or any(not _valid_attack(item) for item in value.attacks)
            or len(value.warnings) > 100
            or any(not isinstance(item, str) or len(item) > 500
                   for item in value.warnings)
        ):
            raise ValueError("Invalid CWL bonus scope report")
        object.__setattr__(self, "retained_bytes", len(json.dumps(
            self.storage_payload(), ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")))

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "guild_id": self.guild_id,
            "snapshot": asdict(self.snapshot),
        }

    def manifest(self) -> dict[str, Any]:
        value = self.snapshot
        return {
            "report_id": self.report_id, "kind": "cwl_bonus_scope",
            "observed_at": value.observed_at, "clan_code": value.clan_code,
            "season": value.season, "scope_type": value.scope_type,
            "requested_round": value.requested_round,
            "requested_war_tag": value.requested_war_tag,
            "resolved_rounds": list(value.resolved_rounds),
            "resolved_war_tags": list(value.resolved_war_tags),
            "coverage_status": value.coverage_status,
            "settings": asdict(value.settings),
            "summary_count": len(value.summaries),
            "ineligible_count": len(value.ineligible),
            "attack_count": len(value.attacks),
            "warnings": list(value.warnings),
        }

    def page(self, *, offset: int = 0, limit: int = 25) -> dict[str, Any]:
        if (
            type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 25
        ):
            raise ValueError("Invalid CWL bonus scope page")
        value = self.snapshot
        rows = value.summaries if value.scope_type == "season" else value.attacks
        return {
            **self.manifest(),
            "metric_name": "Configured CWL bonus adjusted delta",
            "metric_definition": (
                "Actual contribution minus the configured Town Hall matchup "
                "expectation, plus the configured uphit/downhit adjustment."
            ),
            "actual_score_definition": (
                "Three stars scores 3.00; lower results combine stars and "
                "destruction. Repeated hits receive only additional improvement "
                "unless the attack gains at least two stars."
            ),
            "ass_distinction": (
                "This configured bonus metric is not ASS and must not be "
                "presented as an ASS score."
            ),
            "matching_rows": len(rows),
            "rows": [asdict(row) for row in rows[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "ineligible": [asdict(row) for row in value.ineligible],
            "complete_snapshot": True,
        }


def _finite(*values: Any) -> bool:
    return all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        for value in values
    )


def _valid_settings(value: Any) -> bool:
    return bool(
        type(value.revision) is int and value.revision >= 0
        and (value.updated_at is None or isinstance(value.updated_at, str)
             and bool(value.updated_at) and len(value.updated_at) <= 64)
        and type(value.max_downhit) is int and value.max_downhit >= 0
        and type(value.max_uphit) is int and value.max_uphit >= 0
        and type(value.downhit_severe_after) is int
        and value.downhit_severe_after >= 0
        and _finite(
            value.downhit_penalty_per_level, value.uphit_bonus_per_level,
            value.downhit_severe_base, value.downhit_severe_multiplier,
        )
        and value.downhit_penalty_per_level >= 0
        and value.uphit_bonus_per_level >= 0
        and value.downhit_severe_base >= 0
        and value.downhit_severe_multiplier >= 1
    )


def _valid_summary(value: Any) -> bool:
    return bool(
        normalize_player_tag(value.player_tag) == value.player_tag
        and isinstance(value.player_name, str) and bool(value.player_name)
        and type(value.rank) is int and value.rank > 0
        and type(value.attack_count) is int and value.attack_count > 0
        and type(value.missed_attacks) is int and value.missed_attacks >= 0
        and _finite(
            value.average_adjusted_delta, value.total_adjusted_delta,
            value.total_actual, value.total_expected, value.total_base_delta,
            value.total_adjustment,
        )
    )


def _valid_ineligible(value: Any) -> bool:
    return bool(
        normalize_player_tag(value.player_tag) == value.player_tag
        and isinstance(value.player_name, str) and bool(value.player_name)
        and all(type(item) is int and item >= 0 for item in (
            value.missed_attacks, value.expected_attacks, value.used_attacks,
        ))
        and isinstance(value.reason, str) and bool(value.reason)
        and len(value.reason) <= 200
    )


def _valid_attack(value: Any) -> bool:
    return bool(
        type(value.cwl_round) is int and 1 <= value.cwl_round <= 7
        and isinstance(value.war_tag, str) and bool(value.war_tag)
        and len(value.war_tag) <= 100
        and normalize_player_tag(value.player_tag) == value.player_tag
        and isinstance(value.player_name, str) and bool(value.player_name)
        and type(value.attacker_townhall) is int
        and 1 <= value.attacker_townhall <= 20
        and isinstance(value.defender_tag, str)
        and type(value.defender_townhall) is int
        and 1 <= value.defender_townhall <= 20
        and type(value.stars) is int and 0 <= value.stars <= 3
        and type(value.townhall_difference) is int
        and isinstance(value.expected_lookup, str) and bool(value.expected_lookup)
        and type(value.star_gain) is int and 0 <= value.star_gain <= 3
        and isinstance(value.flags, str) and len(value.flags) <= 200
        and _finite(
            value.destruction, value.actual_score, value.expected_score,
            value.base_delta, value.adjustment, value.adjusted_delta,
        )
        and 0 <= value.destruction <= 100
    )


__all__ = ["CwlBonusScopeReport"]
