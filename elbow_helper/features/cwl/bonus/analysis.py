"""CWL bonus scoring analysis over stored league wars."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple
from typing import Protocol

from elbow_helper.data.collectors import ProfileDemand
from elbow_helper.configuration.clans import CLAN_NAMES
from ..config import CLAN_DELTA_STAR_MULTIPLIERS

LOGGER = logging.getLogger(__name__)
# On a dashboard cache miss, a family profile from the collectors' hourly pass is recent enough.
PROFILE_MAX_AGE_SECONDS = 60 * 60


def bonus_ranking_key(row):
    return (
        -float(row["avg_adjusted_delta"]),
        -float(row["total_adjusted_delta"]),
        -float(row["total_actual"]),
        -int(row["attack_count"]),
        str(row["player_name"]).lower(),
    )


class BonusWarSource(Protocol):
    def bonus_seasons(
        self,
        clan_codes: list[str] | None = None,
    ) -> list[str]: ...

    def bonus_wars(
        self,
        clan_code: str,
        season: str,
    ) -> list[dict[str, Any]]: ...


class LeagueGroupSource(Protocol):
    async def current_wars(
        self,
        clan_code: str,
    ) -> Tuple[List[Dict[str, Any]], List[str]]: ...


class BonusAnalysisService:
    """Load CWL war data and calculate bonus scoring results."""

    def __init__(
        self,
        profiles: ProfileDemand,
        repository: BonusWarSource,
        groups: LeagueGroupSource | None = None,
    ):
        # The collectors serve the dashboards' player and clan profiles; league data comes from the record.
        self._profiles = profiles
        self._repository = repository
        self._groups = groups

    async def fetch_profile(
        self,
        kind: str,
        tag: str,
        *,
        retries: int = 4,
    ) -> Optional[Dict[str, Any]]:
        """A player or clan profile from the last hour, or None when it can't be had."""
        ask = self._profiles.player if kind == "player" else self._profiles.clan
        result = await ask(
            tag,
            max_age=PROFILE_MAX_AGE_SECONDS,
            attempts=retries,
            timeout_seconds=20,
            backoff_seconds=1.0,
            maximum_backoff_seconds=10.0,
        )
        return result.payload


    async def current_wars(
        self,
        clan_code: str,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """The clan's league wars for live dashboard views, as the family record holds them."""
        if self._groups is None:
            return [], [f"{clan_code}: failed to fetch league group."]
        return await self._groups.current_wars(clan_code)


    def seasons(
        self,
        clan_codes: list[str] | None = None,
    ) -> list[str]:
        return self._repository.bonus_seasons(clan_codes)


    def analyze_clan(
        self, clan_code: str, season: str, config: Dict[str, Any],
    ) -> tuple[list, list, list, list[str], list[str]]:
        warnings: List[str] = []
        try:
            wars = self._repository.bonus_wars(clan_code, season)
        except sqlite3.Error:
            LOGGER.exception("Failed loading CWL bonus data clan=%s season=%s", clan_code, season)
            return [], [], [], warnings, [f"{clan_code}: CWL data couldn't be loaded."]
        if not wars:
            warnings.append(f"No completed CWL wars are available for {clan_code} in {season}.")
        summary, ineligible, raw, scoring_warnings, errors = self.analyze_wars(
            clan_code, wars, config,
        )
        return summary, ineligible, raw, warnings + scoring_warnings, errors

    def analyze_wars(
        self, clan_code: str, wars: List[Dict[str, Any]], config: Dict[str, Any],
    ) -> tuple[list, list, list, list[str], list[str]]:
        """Score exactly these wars together using the configured bonus calculation."""
        warnings: List[str] = []
        errors: List[str] = []
        from ..evidence import require_scoring_evidence
        try:
            for war in wars:
                require_scoring_evidence([war], war.get("roster", []), war.get("attacks", []))
        except ValueError:
            return [], [], [], [], [f"{clan_code}: CWL data couldn't be loaded."]
        clan_name = CLAN_NAMES.get(clan_code, clan_code)

        profile = (config.get("clans") or {}).get(clan_code, {})
        matchup_expected = profile.get("matchup_expected", {})
        max_downhit = int(profile.get("max_downhit", 2))
        max_uphit = int(profile.get("max_uphit", 8))
        downhit_penalty_per_level = float(profile.get("downhit_penalty_per_level", 0.15))
        uphit_bonus_per_level = float(profile.get("uphit_bonus_per_level", 0.10))
        # Severe downhit growth starts once downhit levels exceed this threshold.
        downhit_severe_after = int(profile.get("downhit_severe_after", 0))
        downhit_severe_base = float(profile.get("downhit_severe_base", 0.20))
        downhit_severe_multiplier = float(profile.get("downhit_severe_multiplier", 2.0))

        player_stats: Dict[str, Dict[str, Any]] = {}
        raw_rows: List[Dict[str, Any]] = []
        missing_matchups: Set[str] = set()

        for war in wars:
            round_no = int(war.get("cwl_round") or 0)
            war_id = str(war.get("war_id") or "")
            war_tag = war_id.removeprefix("CWL:") or "?"
            members = war.get("roster", []) or []
            attacks = war.get("attacks", []) or []
            attacks_by_player: Dict[str, List[Dict[str, Any]]] = {}
            for attack in attacks:
                attacks_by_player.setdefault(str(attack.get("player_tag") or ""), []).append(attack)
            attacks_per_member = int(war.get("attacks_per_member") or 1)

            def _raw_attack_score(stars: int, destruction: float) -> float:
                if stars >= 3:
                    return 3.0
                return stars + destruction / 100.0

            ordered_attacks: List[Dict[str, Any]] = []
            for member in members:
                player_tag = member.get("player_tag")
                if not player_tag:
                    continue
                stats = player_stats.setdefault(
                    player_tag,
                    {
                        "clan": clan_code,
                        "clan_name": clan_name,
                        "player_tag": player_tag,
                        "player_name": member.get("player_name") or player_tag,
                        "expected_attacks": 0,
                        "used_attacks": 0,
                        "missed_attacks": 0,
                        "attack_count": 0,
                        "total_actual": 0.0,
                        "total_expected": 0.0,
                        "total_base_delta": 0.0,
                        "total_adjustment": 0.0,
                        "total_adjusted_delta": 0.0,
                        "total_clan_delta": 0.0,
                        "flagged_attacks": 0,
                    },
                )
                stats["player_name"] = member.get("player_name") or stats["player_name"]
                expected = int(member.get("attacks_expected") or attacks_per_member)
                stats["expected_attacks"] += expected
                member_attacks = attacks_by_player.get(str(player_tag), [])
                used = int(member.get("attacks_used") or len(member_attacks))
                stats["used_attacks"] += used
                if used < expected:
                    stats["missed_attacks"] += expected - used

                attacker_th = member.get("townhall")
                for attack_index, attack in enumerate(member_attacks):
                    defender_tag = attack.get("defender_tag")
                    defender_th = attack.get("defender_townhall")
                    if (
                        not isinstance(attacker_th, int)
                        or not isinstance(defender_th, int)
                        or attacker_th <= 0
                        or defender_th <= 0
                    ):
                        continue
                    stars = int(attack.get("stars") or 0)
                    destruction = float(attack.get("destruction") or 0.0)
                    destruction = max(0.0, min(100.0, destruction))
                    order_raw = attack.get("attack_order")
                    try:
                        order_value = int(order_raw)
                    except (TypeError, ValueError):
                        order_value = 10**9 + attack_index
                    ordered_attacks.append(
                        {
                            "order": order_value,
                            "player_tag": player_tag,
                            "player_name": stats["player_name"],
                            "attacker_th": attacker_th,
                            "defender_tag": defender_tag or "",
                            "defender_th": defender_th,
                            "stars": stars,
                            "destruction": destruction,
                            "raw_actual_score": _raw_attack_score(stars, destruction),
                        }
                    )

            ordered_attacks.sort(
                key=lambda row: (
                    int(row["order"]),
                    str(row["player_name"]).lower(),
                    str(row["defender_tag"]),
                )
            )

            defender_best_actual: Dict[str, float] = {}
            defender_best_stars: Dict[str, int] = {}
            for attack_row in ordered_attacks:
                defender_key = str(attack_row["defender_tag"])
                previous_best = defender_best_actual.get(defender_key, 0.0)
                previous_best_stars = defender_best_stars.get(defender_key, 0)
                raw_actual_score = float(attack_row["raw_actual_score"])
                current_stars = int(attack_row["stars"])
                star_gain = max(0, current_stars - previous_best_stars)
                # If this attack jumps the base by 2+ stars, give full credit.
                # Otherwise, score only incremental war contribution.
                if star_gain >= 2:
                    actual_score = raw_actual_score
                else:
                    actual_score = max(0.0, raw_actual_score - previous_best)
                if raw_actual_score > previous_best:
                    defender_best_actual[defender_key] = raw_actual_score
                    defender_best_stars[defender_key] = current_stars
                display_stars = int(attack_row["stars"])
                display_destruction = float(attack_row["destruction"])
                if actual_score <= 0.0:
                    # No net gain on this base contribution-wise.
                    display_stars = 0
                    display_destruction = 0.0

                attacker_th = int(attack_row["attacker_th"])
                defender_th = int(attack_row["defender_th"])
                th_gap = defender_th - attacker_th
                lookup_defender_th = defender_th
                if th_gap < -max_downhit:
                    lookup_defender_th = attacker_th - max_downhit
                elif th_gap > max_uphit:
                    lookup_defender_th = attacker_th + max_uphit

                flag_tokens: List[str] = []
                lookup_key = f"{attacker_th}:{lookup_defender_th}"
                expected_score = matchup_expected.get(lookup_key)
                if not isinstance(expected_score, (int, float)):
                    missing_matchups.add(lookup_key)
                    continue

                base_delta = actual_score - float(expected_score)
                adjustment = 0.0
                if actual_score <= 0.0:
                    flag_tokens.append("no_contribution")
                downhit_levels = max(0, -th_gap)
                if downhit_levels > 0:
                    penalty = downhit_levels * downhit_penalty_per_level
                    severe_levels = max(0, downhit_levels - downhit_severe_after)
                    if severe_levels > 0:
                        penalty += downhit_severe_base * (
                            (downhit_severe_multiplier ** severe_levels) - 1.0
                        )
                    adjustment -= penalty
                    flag_tokens.append("downhit_penalty")
                uphit_levels = max(0, th_gap)
                if uphit_levels > 0:
                    bonus = uphit_levels * uphit_bonus_per_level
                    adjustment += bonus
                    flag_tokens.append("uphit_bonus")

                adjusted_delta = base_delta + adjustment
                star_multiplier = self._clan_delta_star_multiplier(display_stars)
                clan_delta = (actual_score * star_multiplier) - float(expected_score) + adjustment
                stats = player_stats[str(attack_row["player_tag"])]
                stats["attack_count"] += 1
                stats["total_actual"] += actual_score
                stats["total_expected"] += float(expected_score)
                stats["total_base_delta"] += base_delta
                stats["total_adjustment"] += adjustment
                stats["total_adjusted_delta"] += adjusted_delta
                stats["total_clan_delta"] += clan_delta
                if flag_tokens:
                    stats["flagged_attacks"] += 1

                raw_rows.append(
                    {
                        "clan": clan_code,
                        "clan_name": clan_name,
                        "round": round_no,
                        "war_tag": war_tag,
                        "player_name": stats["player_name"],
                        "player_tag": attack_row["player_tag"],
                        "attacker_th": attacker_th,
                        "defender_tag": defender_key,
                        "defender_th": defender_th,
                        "stars": display_stars,
                        "destruction": display_destruction,
                        "actual_score": actual_score,
                        "expected_score": float(expected_score),
                        "th_gap": th_gap,
                        "expected_lookup": lookup_key,
                        "base_delta": base_delta,
                        "delta_adjustment": adjustment,
                        "adjusted_delta": adjusted_delta,
                        "star_gain": star_gain,
                        "star_multiplier": star_multiplier,
                        "clan_delta": clan_delta,
                        "flags": self._format_bonus_flags(flag_tokens),
                    }
                )

        if missing_matchups:
            preview = ", ".join(sorted(missing_matchups)[:12])
            suffix = " ..." if len(missing_matchups) > 12 else ""
            errors.append(
                f"{clan_code} is missing Expected Scores for {preview}{suffix}."
            )
            return [], [], [], warnings, errors

        summary_rows: List[Dict[str, Any]] = []
        ineligible_rows: List[Dict[str, Any]] = []

        for stats in player_stats.values():
            missed_attacks = int(stats["missed_attacks"])
            attack_count = int(stats["attack_count"])
            if missed_attacks > 0:
                ineligible_rows.append(
                    {
                        "clan": clan_code,
                        "clan_name": clan_name,
                        "player_name": stats["player_name"],
                        "player_tag": stats["player_tag"],
                        "missed_attacks": missed_attacks,
                        "expected_attacks": int(stats["expected_attacks"]),
                        "used_attacks": int(stats["used_attacks"]),
                        "reason": "Missed one or more CWL attacks",
                    }
                )
                continue
            if attack_count <= 0:
                ineligible_rows.append(
                    {
                        "clan": clan_code,
                        "clan_name": clan_name,
                        "player_name": stats["player_name"],
                        "player_tag": stats["player_tag"],
                        "missed_attacks": missed_attacks,
                        "expected_attacks": int(stats["expected_attacks"]),
                        "used_attacks": int(stats["used_attacks"]),
                        "reason": "No eligible attacks in completed wars",
                    }
                )
                continue
            avg_adjusted_delta = stats["total_adjusted_delta"] / attack_count
            avg_clan_delta = stats["total_clan_delta"] / attack_count
            summary_rows.append(
                {
                    "clan": clan_code,
                    "clan_name": clan_name,
                    "player_name": stats["player_name"],
                    "player_tag": stats["player_tag"],
                    "attack_count": attack_count,
                    "total_actual": stats["total_actual"],
                    "total_expected": stats["total_expected"],
                    "total_base_delta": stats["total_base_delta"],
                    "total_adjustment": stats["total_adjustment"],
                    "total_adjusted_delta": stats["total_adjusted_delta"],
                    "avg_adjusted_delta": avg_adjusted_delta,
                    "total_clan_delta": stats["total_clan_delta"],
                    "avg_clan_delta": avg_clan_delta,
                    "clan_skill_gap": avg_clan_delta - avg_adjusted_delta,
                    "flagged_attacks": stats["flagged_attacks"],
                    "missed_attacks": missed_attacks,
                }
            )

        summary_rows.sort(key=bonus_ranking_key)
        for idx, row in enumerate(summary_rows, start=1):
            row["rank"] = idx

        return summary_rows, ineligible_rows, raw_rows, warnings, errors


    def _format_bonus_flags(self, flag_tokens: List[str]) -> str:
        if not flag_tokens:
            return ""
        flag_labels = {
            "downhit_penalty": "Downhit penalty",
            "uphit_bonus": "Uphit bonus",
            "no_contribution": "No improvement on base",
        }
        labels = [flag_labels.get(token, token) for token in flag_tokens]
        return " | ".join(dict.fromkeys(labels))


    @staticmethod
    def _clan_delta_star_multiplier(stars: int) -> float:
        # Convert stars to a clan-value multiplier used by the secondary metric.
        clamped_stars = max(0, min(3, int(stars)))
        return float(CLAN_DELTA_STAR_MULTIPLIERS.get(clamped_stars, 0.0))
