"""Typed, read-only evidence queries owned by the CWL feature."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Protocol

from .config import CLAN_NAME_TO_CODE, CWL_CLAN_NAMES, CWL_CLAN_TAGS
from .roster.analysis import (
    build_ass_season_metrics,
    build_mega_ass_metrics,
    profiles_for_roster_history,
)


MAX_HISTORY_SEASONS = 12


class CwlHistorySource(Protocol):
    def roster_history(self, history_limit: int | None) -> dict[str, list[dict[str, Any]]]: ...

    def bonus_seasons(
        self, clan_codes: list[str] | None = None,
    ) -> list[str]: ...

    def bonus_wars(
        self, clan_code: str, season: str,
    ) -> list[dict[str, Any]]: ...


class CwlBonusAnalysisSource(Protocol):
    def analyze_clan(
        self, clan_code: str, season: str, config: dict[str, Any],
    ) -> tuple[
        list[dict[str, Any]], list[dict[str, Any]],
        list[dict[str, Any]], list[str], list[str],
    ]: ...


class CwlBonusConfigSource(Protocol):
    def load(self) -> tuple[dict[str, Any] | None, list[str]]: ...


@dataclass(frozen=True, slots=True)
class CwlClanSeasonSummary:
    season: str
    clan_code: str
    league: str
    latest_end_ts: int
    wars: int
    accounts: int
    attacks: int
    attacks_expected: int
    complete: bool


@dataclass(frozen=True, slots=True)
class CwlPerformanceRow:
    season: str
    clan_code: str
    league: str
    profile_key: str
    player_tag: str
    player_name: str
    townhall: int
    wars: int
    attacks: int
    attacks_expected: int
    stars: int
    average_destruction: float | None
    score: float | None
    rank: int | None
    rank_total: int
    multi_season_score: float | None
    multi_season_rank: int | None
    multi_season_rank_total: int


@dataclass(frozen=True, slots=True)
class CwlPerformanceSnapshot:
    observed_at: str
    history_limit: int
    seasons: tuple[str, ...]
    clan_seasons: tuple[CwlClanSeasonSummary, ...]
    rows: tuple[CwlPerformanceRow, ...]


@dataclass(frozen=True, slots=True)
class CwlAssScopeRow:
    player_tag: str
    player_name: str
    townhall: int
    wars: int
    attacks: int
    attacks_expected: int
    stars: int
    average_destruction: float | None
    average_target_position: float | None
    average_target_distance: float | None
    average_defensive_position: float | None
    ass_score: float | None
    rank: int | None
    rank_total: int
    projected_stars: float | None
    missed_stars: float | None
    missed_adjustment: float | None
    difficulty_adjustment: float | None


@dataclass(frozen=True, slots=True)
class CwlAssScopeSnapshot:
    observed_at: str
    clan_code: str
    season: str
    scope_type: str
    requested_round: int | None
    requested_war_id: str | None
    resolved_war_ids: tuple[str, ...]
    resolved_rounds: tuple[int, ...]
    completed_wars: int
    league: str | None
    profile_key: str | None
    profile_label: str | None
    difficulty_weight: float | None
    missed_mode: str | None
    scoring_status: str
    coverage_status: str
    rows: tuple[CwlAssScopeRow, ...]


@dataclass(frozen=True, slots=True)
class CwlBonusSettings:
    revision: int
    updated_at: str | None
    max_downhit: int
    max_uphit: int
    downhit_penalty_per_level: float
    uphit_bonus_per_level: float
    downhit_severe_after: int
    downhit_severe_base: float
    downhit_severe_multiplier: float


@dataclass(frozen=True, slots=True)
class CwlBonusAttackScore:
    cwl_round: int
    war_tag: str
    player_tag: str
    player_name: str
    attacker_townhall: int
    defender_tag: str
    defender_townhall: int
    stars: int
    destruction: float
    actual_score: float
    expected_score: float
    townhall_difference: int
    expected_lookup: str
    base_delta: float
    adjustment: float
    adjusted_delta: float
    star_gain: int
    flags: str


@dataclass(frozen=True, slots=True)
class CwlBonusPlayerSummary:
    player_tag: str
    player_name: str
    rank: int
    attack_count: int
    average_adjusted_delta: float
    total_adjusted_delta: float
    total_actual: float
    total_expected: float
    total_base_delta: float
    total_adjustment: float
    missed_attacks: int


@dataclass(frozen=True, slots=True)
class CwlBonusIneligiblePlayer:
    player_tag: str
    player_name: str
    missed_attacks: int
    expected_attacks: int
    used_attacks: int
    reason: str


@dataclass(frozen=True, slots=True)
class CwlBonusScopeSnapshot:
    observed_at: str
    clan_code: str
    season: str
    scope_type: str
    requested_round: int | None
    requested_war_tag: str | None
    resolved_rounds: tuple[int, ...]
    resolved_war_tags: tuple[str, ...]
    settings: CwlBonusSettings
    summaries: tuple[CwlBonusPlayerSummary, ...]
    ineligible: tuple[CwlBonusIneligiblePlayer, ...]
    attacks: tuple[CwlBonusAttackScore, ...]
    warnings: tuple[str, ...]
    coverage_status: str


@dataclass(frozen=True, slots=True)
class CwlThreadRegistration:
    clan_code: str
    clan_name: str
    clan_tag: str
    thread_id: int
    last_activity: str | None


class CwlQueries:
    """Bounded snapshots for agent and other read consumers."""

    def __init__(
        self,
        history: CwlHistorySource,
        thread_state: Callable[[], Mapping[str, Any]],
        *,
        bonus_analysis: CwlBonusAnalysisSource | None = None,
        bonus_config: CwlBonusConfigSource | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._history = history
        self._thread_state = thread_state
        self._bonus_analysis = bonus_analysis
        self._bonus_config = bonus_config
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def performance(self, *, history_limit: int = 3) -> CwlPerformanceSnapshot:
        if type(history_limit) is not int or not 1 <= history_limit <= MAX_HISTORY_SEASONS:
            raise ValueError("Invalid CWL history limit")
        dataset = self._history.roster_history(history_limit)
        seasons = tuple(str(row["key"]) for row in dataset["seasons"])
        season_order = {
            season: index for index, season in enumerate(reversed(seasons), start=1)
        }
        profiles, latest_leagues = profiles_for_roster_history(dataset["wars"])
        metrics = build_ass_season_metrics(
            wars=dataset["wars"], roster=dataset["roster"], attacks=dataset["attacks"],
            season_order=season_order, profiles_by_clan=profiles,
        )
        multi_season = {
            (metric.clan_code, metric.player_tag): metric
            for metric in build_mega_ass_metrics(metrics)
        }
        groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for metric in metrics:
            groups[(metric.season, metric.clan_code)].append(metric)
        war_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for war in dataset["wars"]:
            war_groups[(str(war.get("cwl_season") or ""), str(war.get("clan_code") or ""))].append(war)

        summaries = []
        for key, group in groups.items():
            season, clan_code = key
            wars = war_groups[key]
            war_count = len({str(war.get("war_id") or "") for war in wars})
            summaries.append(CwlClanSeasonSummary(
                season=season, clan_code=clan_code,
                league=group[0].league or latest_leagues.get(clan_code, "Unknown"),
                latest_end_ts=max((int(war.get("end_ts") or 0) for war in wars), default=0),
                wars=war_count,
                accounts=len(group), attacks=sum(row.attacks for row in group),
                attacks_expected=sum(row.attacks_expected for row in group),
                complete=war_count >= 7,
            ))

        rows = []
        for metric in metrics:
            combined = multi_season.get((metric.clan_code, metric.player_tag))
            rows.append(CwlPerformanceRow(
                season=metric.season, clan_code=metric.clan_code, league=metric.league,
                profile_key=metric.profile.key, player_tag=metric.player_tag,
                player_name=metric.player_name, townhall=metric.townhall, wars=metric.wars,
                attacks=metric.attacks, attacks_expected=metric.attacks_expected, stars=metric.stars,
                average_destruction=metric.average_destruction, score=metric.score,
                rank=metric.rank, rank_total=metric.rank_total,
                multi_season_score=combined.score if combined else None,
                multi_season_rank=combined.rank if combined else None,
                multi_season_rank_total=combined.rank_total if combined else 0,
            ))

        clan_order = {code: index for index, code in enumerate(CWL_CLAN_NAMES)}
        summaries.sort(key=lambda row: (-season_order.get(row.season, 0), clan_order.get(row.clan_code, 999), row.clan_code))
        rows.sort(key=lambda row: (
            -season_order.get(row.season, 0), clan_order.get(row.clan_code, 999),
            row.rank if row.rank is not None else 999_999, row.player_name.casefold(), row.player_tag,
        ))
        observed_at = self._clock()
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        return CwlPerformanceSnapshot(
            observed_at=observed_at.astimezone(timezone.utc).isoformat(),
            history_limit=history_limit, seasons=seasons,
            clan_seasons=tuple(summaries), rows=tuple(rows),
        )

    def ass_seasons(self, *, clan_code: str) -> tuple[str, ...]:
        if clan_code not in CWL_CLAN_NAMES:
            raise ValueError("Invalid CWL clan code")
        return tuple(dict.fromkeys(
            season for season in self._history.bonus_seasons([clan_code])
            if isinstance(season, str)
            and re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", season)
        ))

    def ass_scope(
        self, *, clan_code: str, season: str, scope_type: str,
        cwl_round: int | None = None, war_id: str | None = None,
    ) -> CwlAssScopeSnapshot:
        """Calculate projected ASS from the exact selected stored CWL scope."""
        if clan_code not in CWL_CLAN_NAMES:
            raise ValueError("Invalid CWL clan code")
        if (
            not isinstance(season, str)
            or re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", season) is None
            or scope_type not in {"season", "round", "war"}
            or cwl_round is not None
            and (type(cwl_round) is not int or not 1 <= cwl_round <= 7)
            or war_id is not None
            and (not isinstance(war_id, str) or not war_id or len(war_id) > 100)
            or scope_type == "season" and (cwl_round is not None or war_id is not None)
            or scope_type == "round" and (cwl_round is None or war_id is not None)
            or scope_type == "war" and (war_id is None or cwl_round is not None)
        ):
            raise ValueError("Invalid CWL ASS scope")

        season_wars = self._history.bonus_wars(clan_code, season)
        if scope_type == "round":
            selected = [
                war for war in season_wars
                if int(war.get("cwl_round") or 0) == cwl_round
            ]
        elif scope_type == "war":
            selected = [
                war for war in season_wars
                if str(war.get("war_id") or "") == war_id
                or str(war.get("war_id") or "").removeprefix("CWL:")
                == war_id
            ]
        else:
            selected = list(season_wars)

        observed = self._clock()
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        resolved_war_ids = tuple(sorted(
            str(war.get("war_id") or "") for war in selected
            if str(war.get("war_id") or "")
        ))
        resolved_rounds = tuple(sorted({
            int(war.get("cwl_round") or 0) for war in selected
            if int(war.get("cwl_round") or 0) > 0
        }))
        if not selected:
            return CwlAssScopeSnapshot(
                observed.astimezone(timezone.utc).isoformat(), clan_code,
                season, scope_type, cwl_round, war_id, (), (), 0,
                None, None, None, None, None, "unavailable",
                "no_matching_completed_wars", (),
            )

        wars, roster, attacks = _flatten_bonus_wars(selected, clan_code)
        profiles, leagues = profiles_for_roster_history(wars)
        profile = profiles.get(clan_code)
        metrics = build_ass_season_metrics(
            wars=wars, roster=roster, attacks=attacks,
            season_order={season: 1}, profiles_by_clan=profiles,
        ) if profile is not None else []
        rows = tuple(CwlAssScopeRow(
            player_tag=metric.player_tag,
            player_name=metric.player_name,
            townhall=metric.townhall,
            wars=metric.wars,
            attacks=metric.attacks,
            attacks_expected=metric.attacks_expected,
            stars=metric.stars,
            average_destruction=metric.average_destruction,
            average_target_position=metric.average_target_position,
            average_target_distance=metric.average_target_distance,
            average_defensive_position=metric.average_defensive_position,
            ass_score=metric.score,
            rank=metric.rank,
            rank_total=metric.rank_total,
            projected_stars=metric.projected_stars,
            missed_stars=metric.missed_stars,
            missed_adjustment=metric.missed_adjustment,
            difficulty_adjustment=metric.difficulty_adjustment,
        ) for metric in sorted(metrics, key=lambda item: (
            item.rank if item.rank is not None else 999_999,
            item.player_name.casefold(), item.player_tag,
        )))
        scoring_status = "calculated_from_selected_scope"
        coverage_status = (
            "selected_season_completed_wars"
            if scope_type == "season" else "selected_completed_wars"
        )
        return CwlAssScopeSnapshot(
            observed.astimezone(timezone.utc).isoformat(), clan_code,
            season, scope_type, cwl_round, war_id, resolved_war_ids,
            resolved_rounds, len(resolved_war_ids),
            leagues.get(clan_code), profile.key if profile else None,
            profile.label if profile else None,
            profile.difficulty_weight if profile else None,
            profile.missed_mode if profile else None,
            scoring_status, coverage_status, rows,
        )

    def bonus_scope(
        self, *, clan_code: str, season: str, scope_type: str,
        cwl_round: int | None = None, war_tag: str | None = None,
    ) -> CwlBonusScopeSnapshot:
        """Apply configured bonus scoring, then select an exact stored scope."""
        if self._bonus_analysis is None or self._bonus_config is None:
            raise RuntimeError("CWL bonus scoring is unavailable")
        if (
            clan_code not in CWL_CLAN_NAMES
            or not isinstance(season, str)
            or re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", season) is None
            or scope_type not in {"season", "round", "war"}
            or cwl_round is not None
            and (type(cwl_round) is not int or not 1 <= cwl_round <= 7)
            or war_tag is not None
            and (not isinstance(war_tag, str) or not war_tag or len(war_tag) > 100)
            or scope_type == "season" and (cwl_round is not None or war_tag is not None)
            or scope_type == "round" and (cwl_round is None or war_tag is not None)
            or scope_type == "war" and (war_tag is None or cwl_round is not None)
        ):
            raise ValueError("Invalid CWL bonus scope")
        config, config_errors = self._bonus_config.load()
        if config is None:
            raise ValueError(
                "CWL bonus scoring settings are unavailable: "
                + " | ".join(config_errors[:4])
            )
        clan_settings = (config.get("clans") or {}).get(clan_code)
        if not isinstance(clan_settings, dict):
            raise ValueError("CWL bonus scoring settings are unavailable")
        summary, ineligible, raw, warnings, errors = (
            self._bonus_analysis.analyze_clan(clan_code, season, config)
        )
        if errors:
            raise ValueError("CWL bonus scoring is incomplete: " + " | ".join(errors[:4]))

        def matches(row: dict[str, Any]) -> bool:
            if scope_type == "round":
                return int(row.get("round") or 0) == cwl_round
            if scope_type == "war":
                stored = str(row.get("war_tag") or "")
                return stored == war_tag or f"CWL:{stored}" == war_tag
            return True

        selected = [row for row in raw if matches(row)]
        resolved_rounds = tuple(sorted({int(row["round"]) for row in selected}))
        resolved_war_tags = tuple(sorted({str(row["war_tag"]) for row in selected}))
        settings = CwlBonusSettings(
            revision=int(config.get("revision") or 0),
            updated_at=str(
                ((config.get("clan_meta") or {}).get(clan_code) or {}).get(
                    "updated_at_utc"
                ) or ""
            ) or None,
            max_downhit=int(clan_settings["max_downhit"]),
            max_uphit=int(clan_settings["max_uphit"]),
            downhit_penalty_per_level=float(
                clan_settings["downhit_penalty_per_level"]
            ),
            uphit_bonus_per_level=float(clan_settings["uphit_bonus_per_level"]),
            downhit_severe_after=int(clan_settings["downhit_severe_after"]),
            downhit_severe_base=float(clan_settings["downhit_severe_base"]),
            downhit_severe_multiplier=float(
                clan_settings["downhit_severe_multiplier"]
            ),
        )
        attacks = tuple(CwlBonusAttackScore(
            cwl_round=int(row["round"]), war_tag=str(row["war_tag"]),
            player_tag=str(row["player_tag"]),
            player_name=str(row["player_name"]),
            attacker_townhall=int(row["attacker_th"]),
            defender_tag=str(row["defender_tag"]),
            defender_townhall=int(row["defender_th"]),
            stars=int(row["stars"]), destruction=float(row["destruction"]),
            actual_score=float(row["actual_score"]),
            expected_score=float(row["expected_score"]),
            townhall_difference=int(row["th_gap"]),
            expected_lookup=str(row["expected_lookup"]),
            base_delta=float(row["base_delta"]),
            adjustment=float(row["delta_adjustment"]),
            adjusted_delta=float(row["adjusted_delta"]),
            star_gain=int(row["star_gain"]), flags=str(row["flags"] or ""),
        ) for row in selected)
        summaries = tuple(CwlBonusPlayerSummary(
            player_tag=str(row["player_tag"]),
            player_name=str(row["player_name"]), rank=int(row["rank"]),
            attack_count=int(row["attack_count"]),
            average_adjusted_delta=float(row["avg_adjusted_delta"]),
            total_adjusted_delta=float(row["total_adjusted_delta"]),
            total_actual=float(row["total_actual"]),
            total_expected=float(row["total_expected"]),
            total_base_delta=float(row["total_base_delta"]),
            total_adjustment=float(row["total_adjustment"]),
            missed_attacks=int(row["missed_attacks"]),
        ) for row in summary) if scope_type == "season" else ()
        excluded = tuple(CwlBonusIneligiblePlayer(
            player_tag=str(row["player_tag"]),
            player_name=str(row["player_name"]),
            missed_attacks=int(row["missed_attacks"]),
            expected_attacks=int(row["expected_attacks"]),
            used_attacks=int(row["used_attacks"]), reason=str(row["reason"]),
        ) for row in ineligible) if scope_type == "season" else ()
        observed = self._clock()
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return CwlBonusScopeSnapshot(
            observed.astimezone(timezone.utc).isoformat(), clan_code, season,
            scope_type, cwl_round, war_tag, resolved_rounds,
            resolved_war_tags, settings, summaries, excluded, attacks,
            tuple(str(value) for value in warnings),
            "stored_season_scoring" if scope_type == "season" and attacks
            else "selected_scored_attacks" if attacks
            else "no_matching_scored_attacks",
        )

    def registered_threads(self) -> tuple[CwlThreadRegistration, ...]:
        registrations = []
        for raw_thread_id, value in self._thread_state().items():
            if not isinstance(value, Mapping):
                continue
            clan_name = str(value.get("clan_name") or "")
            clan_code = CLAN_NAME_TO_CODE.get(clan_name)
            try:
                thread_id = int(raw_thread_id)
            except (TypeError, ValueError):
                continue
            if clan_code is None or thread_id <= 0:
                continue
            last_activity = value.get("last_activity")
            registrations.append(CwlThreadRegistration(
                clan_code=clan_code, clan_name=clan_name,
                clan_tag=CWL_CLAN_TAGS[clan_code], thread_id=thread_id,
                last_activity=str(last_activity) if last_activity else None,
            ))
        registrations.sort(key=lambda row: tuple(CWL_CLAN_NAMES).index(row.clan_code))
        return tuple(registrations)



def _flatten_bonus_wars(
    source: list[dict[str, Any]], clan_code: str,
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
]:
    wars: list[dict[str, Any]] = []
    roster: list[dict[str, Any]] = []
    attacks: list[dict[str, Any]] = []
    for value in source:
        war_id = str(value.get("war_id") or "")
        wars.append({
            key: item for key, item in value.items()
            if key not in {"roster", "attacks"}
        })
        roster.extend({
            **item, "war_id": war_id, "clan_code": clan_code,
        } for item in value.get("roster", ()))
        attacks.extend({
            **item, "war_id": war_id, "clan_code": clan_code,
        } for item in value.get("attacks", ()))
    return wars, roster, attacks
