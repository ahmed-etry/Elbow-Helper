"""Complete historical CWL performance snapshots with bounded pages."""

from dataclasses import asdict, dataclass, field
import json
import math
import re
from typing import Any

from elbow_helper.configuration.clans import CLAN_ORDER
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.features.cwl.queries import (
    CwlAssScopeSnapshot, CwlPerformanceSnapshot,
)
from elbow_helper.features.cwl.queries import MAX_HISTORY_SEASONS


@dataclass(frozen=True, slots=True)
class CwlPerformanceReport:
    report_id: str
    guild_id: int
    snapshot: CwlPerformanceSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        seasons = self.snapshot.seasons
        if type(self.guild_id) is not int or self.guild_id <= 0:
            raise ValueError("Invalid CWL report guild")
        if not isinstance(self.report_id, str) or not self.report_id or not isinstance(self.snapshot.observed_at, str) or not self.snapshot.observed_at:
            raise ValueError("Invalid CWL report identity")
        if type(self.snapshot.history_limit) is not int or not 1 <= self.snapshot.history_limit <= MAX_HISTORY_SEASONS:
            raise ValueError("Invalid CWL report history limit")
        if len(seasons) != len(set(seasons)) or any(not isinstance(season, str) or not season for season in seasons):
            raise ValueError("Invalid CWL report seasons")
        if any(row.season not in seasons or row.clan_code not in CLAN_ORDER
               for row in (*self.snapshot.clan_seasons, *self.snapshot.rows)):
            raise ValueError("CWL report rows do not belong to the snapshot")
        payload = {"report_id": self.report_id, "guild_id": self.guild_id,
                   "snapshot": asdict(self.snapshot)}
        object.__setattr__(self, "retained_bytes", len(json.dumps(
            payload, ensure_ascii=False,
        ).encode("utf-8")))

    def manifest(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "kind": "cwl_performance",
            "observed_at": self.snapshot.observed_at,
            "history_limit": self.snapshot.history_limit,
            "seasons": list(self.snapshot.seasons),
            "row_count": len(self.snapshot.rows),
        }

    def page(
        self, *, season: str | None = None, clan_code: str | None = None,
        player_tag: str | None = None, offset: int = 0, limit: int = 25,
    ) -> dict[str, Any]:
        rows = tuple(row for row in self.snapshot.rows if (
            (season is None or row.season == season)
            and (clan_code is None or row.clan_code == clan_code)
            and (player_tag is None or row.player_tag == player_tag)
        ))
        summaries = tuple(row for row in self.snapshot.clan_seasons if (
            (season is None or row.season == season)
            and (clan_code is None or row.clan_code == clan_code)
        ))
        return {
            **self.manifest(),
            "filters": {"season": season, "clan_code": clan_code, "player_tag": player_tag},
            "matching_rows": len(rows),
            "matching_accounts": len({row.player_tag for row in rows}),
            "attacks": sum(row.attacks for row in rows),
            "attacks_expected": sum(row.attacks_expected for row in rows),
            "clan_seasons": [asdict(row) for row in summaries],
            "players": [asdict(row) for row in rows[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "complete_snapshot": True,
        }


@dataclass(frozen=True, slots=True)
class CwlAssScopeReport:
    report_id: str
    guild_id: int
    snapshot: CwlAssScopeSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        snapshot = self.snapshot
        scope_identity_valid = (
            snapshot.scope_type == "season"
            and snapshot.requested_round is None
            and snapshot.requested_war_id is None
            or snapshot.scope_type == "round"
            and type(snapshot.requested_round) is int
            and 1 <= snapshot.requested_round <= 7
            and snapshot.requested_war_id is None
            or snapshot.scope_type == "war"
            and snapshot.requested_round is None
            and isinstance(snapshot.requested_war_id, str)
            and bool(snapshot.requested_war_id)
            and len(snapshot.requested_war_id) <= 100
        )
        expected_scoring_status = {
            "calculated_from_selected_scope", "unavailable",
        }
        if (
            not isinstance(self.report_id, str) or not self.report_id
            or len(self.report_id) > 32
            or type(self.guild_id) is not int or self.guild_id <= 0
            or not isinstance(snapshot.observed_at, str)
            or not snapshot.observed_at
            or snapshot.clan_code not in CLAN_ORDER
            or re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", snapshot.season)
            is None
            or not scope_identity_valid
            or snapshot.scoring_status not in expected_scoring_status
            or snapshot.coverage_status not in {
                "selected_season_completed_wars",
                "selected_completed_wars", "no_matching_completed_wars",
            }
            or len(snapshot.resolved_war_ids)
            != len(set(snapshot.resolved_war_ids))
            or any(
                not isinstance(value, str) or not value or len(value) > 100
                for value in snapshot.resolved_war_ids
            )
            or len(snapshot.resolved_rounds)
            != len(set(snapshot.resolved_rounds))
            or any(
                type(value) is not int or not 1 <= value <= 7
                for value in snapshot.resolved_rounds
            )
            or type(snapshot.completed_wars) is not int
            or snapshot.completed_wars != len(snapshot.resolved_war_ids)
            or len(snapshot.rows) > 500
            or any(not _valid_ass_scope_row(row) for row in snapshot.rows)
            or snapshot.coverage_status == "no_matching_completed_wars"
            and (
                snapshot.completed_wars != 0 or snapshot.rows
                or snapshot.scoring_status != "unavailable"
            )
            or snapshot.scoring_status == "calculated_from_selected_scope"
            and (
                snapshot.coverage_status == "no_matching_completed_wars"
                or snapshot.scope_type == "season"
                and snapshot.coverage_status != "selected_season_completed_wars"
                or snapshot.scope_type != "season"
                and snapshot.coverage_status != "selected_completed_wars"
            )
        ):
            raise ValueError("Invalid scoped CWL ASS report")
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
        snapshot = self.snapshot
        return {
            "report_id": self.report_id, "kind": "cwl_ass_scope",
            "observed_at": snapshot.observed_at,
            "clan_code": snapshot.clan_code, "season": snapshot.season,
            "scope_type": snapshot.scope_type,
            "requested_round": snapshot.requested_round,
            "requested_war_id": snapshot.requested_war_id,
            "resolved_war_ids": list(snapshot.resolved_war_ids),
            "resolved_rounds": list(snapshot.resolved_rounds),
            "completed_wars": snapshot.completed_wars,
            "league": snapshot.league,
            "profile_key": snapshot.profile_key,
            "profile_label": snapshot.profile_label,
            "difficulty_weight": snapshot.difficulty_weight,
            "missed_mode": snapshot.missed_mode,
            "scoring_status": snapshot.scoring_status,
            "coverage_status": snapshot.coverage_status,
            "projection_attack_target": 7,
            "observed_attack_count": sum(row.attacks for row in snapshot.rows),
            "scored_account_count": sum(
                row.ass_score is not None for row in snapshot.rows
            ),
            "row_count": len(snapshot.rows),
        }

    def page(self, *, offset: int = 0, limit: int = 25) -> dict[str, Any]:
        if (
            type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 25
        ):
            raise ValueError("Invalid scoped CWL ASS page")
        rows = self.snapshot.rows
        return {
            **self.manifest(),
            "metric_name": "Selected-scope projected ASS",
            "metric_definition": (
                "ASS is a standardized measure of how an account's attacks "
                "contributed relative to teammates in the selected same-clan "
                "scope. It is not a universal skill score."
            ),
            "formula": (
                "(projected stars + missed adjustment + difficulty "
                "adjustment) x average destruction rate"
            ),
            "projection_note": (
                "Each account's observed attack averages in the selected "
                "scope are projected to seven attacks. The result is labelled "
                "with that scope and sample; it is not automatically a "
                "completed-season score."
            ),
            "matching_rows": len(rows),
            "players": [asdict(row) for row in rows[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "complete_snapshot": True,
        }


def _valid_ass_scope_row(row: Any) -> bool:
    numeric = (
        row.average_destruction, row.average_target_position,
        row.average_target_distance, row.average_defensive_position,
        row.ass_score, row.projected_stars, row.missed_stars,
        row.missed_adjustment, row.difficulty_adjustment,
    )
    return bool(
        normalize_player_tag(row.player_tag) == row.player_tag
        and isinstance(row.player_name, str) and row.player_name
        and type(row.townhall) is int and 0 <= row.townhall <= 20
        and all(
            type(value) is int and value >= 0
            for value in (
                row.wars, row.attacks, row.attacks_expected, row.stars,
                row.rank_total,
            )
        )
        and (row.rank is None or type(row.rank) is int and row.rank > 0)
        and (row.ass_score is None) == (row.rank is None)
        and (row.rank is None) == (row.rank_total == 0)
        and (
            row.ass_score is None
            and all(value is None for value in (
                row.projected_stars, row.missed_stars,
                row.missed_adjustment, row.difficulty_adjustment,
            ))
            or row.ass_score is not None
            and all(value is not None for value in (
                row.projected_stars, row.missed_stars,
                row.missed_adjustment, row.difficulty_adjustment,
            ))
        )
        and all(
            value is None
            or isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            for value in numeric
        )
    )
