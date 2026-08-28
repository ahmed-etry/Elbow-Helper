"""Verdict and issue scoring for clan-health rows."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from ..player_health_config import PROFILE_GRADE_BUCKETS, effective_player_rules, raid_scoring_enabled
from ..seasons import _clan_games_window_state, _raid_weekend_window_state
from .verdicts import (
    GOOD,
    INSUFFICIENT_DATA,
    NEEDS_REVIEW,
    NOT_TRACKED,
    WATCH,
    _profile_bucket_signals,
    assess_missed_attack_rate,
    aggregate_overall_verdict,
    bullet_lines,
    clan_games_assessment,
    fmt_pct,
    per_member_action,
    progression_assessment,
    raid_assessment,
    status_counts,
    support_assessment,
)


class ClanHealthFlagMixin:
    @staticmethod
    def _cwl_assessment(
        *,
        allowed_miss_rate_percent: float,
        joined: int,
        available: int,
        expected: int,
        used: int,
    ) -> Dict[str, Any]:
        missed = max(0, expected - used)
        if available <= 0:
            participation_verdict = NOT_TRACKED
            participation_reason = "CWL did not run during this period."
        elif joined >= available:
            participation_verdict = GOOD
            participation_reason = f"Joined all {available} CWL wars"
        elif joined > 0:
            participation_verdict = WATCH
            participation_reason = f"Joined {joined} of {available} CWL wars"
        else:
            participation_verdict = NEEDS_REVIEW
            participation_reason = "No CWL during this period"

        if expected <= 0:
            hit_usage_verdict = NOT_TRACKED if participation_verdict == NOT_TRACKED else INSUFFICIENT_DATA
            hit_usage_reason = (
                "No CWL attacks to review during this period."
                if participation_verdict == NOT_TRACKED
                else "Not enough CWL attack history is available for this period."
            )
            missed_rate_percent = None
        else:
            usage_result = assess_missed_attack_rate(
                expected=expected,
                used=used,
                allowed_rate_percent=allowed_miss_rate_percent,
            )
            hit_usage_verdict = usage_result["verdict"]
            hit_usage_reason = usage_result["reason"].replace("war attacks", "CWL attacks")
            missed_rate_percent = usage_result["missed_rate_percent"]

        return {
            "participation_verdict": participation_verdict,
            "participation_reason": participation_reason,
            "hit_usage_verdict": hit_usage_verdict,
            "hit_usage_reason": hit_usage_reason,
            "missed_hits": missed,
            "missed_rate_percent": missed_rate_percent,
            "allowed_miss_rate_percent": float(allowed_miss_rate_percent),
        }

    def _apply_flags(
        self,
        players: List[Dict[str, Any]],
        *,
        cycle_start: datetime | None = None,
        cycle_end: datetime | None = None,
    ) -> None:
        cg_window_state = _clan_games_window_state(cycle_start=cycle_start, cycle_end=cycle_end) if cycle_start and cycle_end else "N/A"
        raid_window_state = _raid_weekend_window_state(cycle_start=cycle_start, cycle_end=cycle_end) if cycle_start and cycle_end else "N/A"
        clan_war_counts_by_type: Dict[str, Dict[str, int]] = {}
        family_cwl_available = 0
        if cycle_start and cycle_end:
            clan_war_counts_by_type = self.repository.clan_war_counts(
                cycle_start_ts=int(cycle_start.timestamp()),
                cycle_end_ts=int(cycle_end.timestamp()),
                clan_codes={str(r.get("clan_code") or "") for r in players if str(r.get("clan_code") or "")},
            )
            family_counts = self.repository.clan_war_counts(
                cycle_start_ts=int(cycle_start.timestamp()),
                cycle_end_ts=int(cycle_end.timestamp()),
            )
            family_cwl_available = max(
                (int(counts.get("cwl") or 0) for counts in family_counts.values()),
                default=0,
            )

        for row in players:
            clan_code = str(row.get("clan_code") or "")
            rules = effective_player_rules(clan_code)
            raid_enabled = raid_scoring_enabled(rules)
            profile_name = str(rules.get("_profile_name") or "casual").strip().lower()
            row["clan_profile"] = profile_name

            available_counts = clan_war_counts_by_type.get(clan_code, {})
            regular_available = int(available_counts.get("regular") or 0)
            cwl_available = family_cwl_available

            regular_result = self._assess_war_participation(
                profile_rules=rules,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
                war_events_joined=int(row.get("regular_war_events_joined") or 0),
                war_expected=int(row.get("regular_war_hits_expected") or 0),
                war_used=int(row.get("regular_war_hits_used") or 0),
                available_wars_in_window=regular_available,
            )

            allowed_miss_rate_percent = float((rules.get("war") or {}).get("missed_attack_rate_percent") or 0)
            cwl_result = self._cwl_assessment(
                allowed_miss_rate_percent=allowed_miss_rate_percent,
                joined=int(row.get("cwl_events_joined") or 0),
                available=cwl_available,
                expected=int(row.get("cwl_hits_expected") or 0),
                used=int(row.get("cwl_hits_used") or 0),
            )

            weekends_joined = int(row.get("raid_weekends_joined") or 0)
            weekends_available = int(row.get("raid_weekends_window") or 0)
            raid_result = raid_assessment(
                profile_rules=rules,
                raid_enabled=raid_enabled,
                raid_used=row.get("raid_attacks"),
                raid_loot=row.get("raid_loot"),
                window_state=raid_window_state,
                weekends_joined=weekends_joined,
                weekends_available=weekends_available,
            )

            cg_result = clan_games_assessment(
                clan_games_rules=rules.get("clan_games", {}),
                cg_delta=row.get("games_delta"),
                window_state=cg_window_state if not bool(row.get("cg_signal_disabled")) else "N/A",
            )

            donation_result = support_assessment(
                donations=row.get("donations"),
                received=row.get("donations_received"),
                clan_code=clan_code,
            )

            progression_result = progression_assessment(
                th_delta=row.get("th_delta"),
                hero_delta=row.get("hero_delta"),
                pet_delta=row.get("pet_delta"),
                equipment_delta=row.get("equipment_delta"),
                troop_delta=row.get("troop_delta"),
                spell_delta=row.get("spell_delta"),
            )

            signal_cards = {
                "war_attendance": {
                    "name": "War participation",
                    "verdict": regular_result["attendance_verdict"],
                    "reason": regular_result["attendance_reason"],
                    "current": f"Joined {int(row.get('regular_war_events_joined') or 0)} of {regular_available} regular wars",
                    "target": (
                        f"Wars to join: {int(regular_result.get('wars_required') or 0)}"
                        if int((rules.get("war") or {}).get("wars_to_join") or 0) > 0
                        else "Context only"
                    ),
                    "action": per_member_action(regular_result["attendance_verdict"]),
                    "graded": profile_name == "competitive" and int((rules.get("war") or {}).get("wars_to_join") or 0) > 0,
                },
                "war_hit_usage": {
                    "name": "War attacks",
                    "verdict": regular_result["hit_usage_verdict"],
                    "reason": regular_result["hit_usage_reason"],
                    "current": (
                        f"Missed {int(regular_result.get('missed_hits') or 0)} of {int(row.get('regular_war_hits_expected') or 0)} regular-war attacks"
                        if int(row.get("regular_war_hits_expected") or 0) > 0
                        else "No regular-war attacks during this period"
                    ),
                    "target": (
                        f"Maximum missed attack rate: {fmt_pct(regular_result.get('allowed_missed_attack_rate_percent') or 0)}"
                        if int(row.get("regular_war_hits_expected") or 0) > 0
                        else "Context only"
                    ),
                    "action": per_member_action(regular_result["hit_usage_verdict"]),
                    "graded": profile_name == "competitive" and int((rules.get("war") or {}).get("wars_to_join") or 0) > 0,
                },
                "cwl_participation": {
                    "name": "CWL participation",
                    "verdict": cwl_result["participation_verdict"],
                    "reason": cwl_result["participation_reason"],
                    "current": f"Joined {int(row.get('cwl_events_joined') or 0)} of {cwl_available} CWL wars",
                    "target": "No fixed target",
                    "action": per_member_action(cwl_result["participation_verdict"]),
                    "graded": profile_name != "utility",
                },
                "cwl_hit_usage": {
                    "name": "CWL attacks",
                    "verdict": cwl_result["hit_usage_verdict"],
                    "reason": cwl_result["hit_usage_reason"],
                    "current": (
                        f"Missed {int(cwl_result.get('missed_hits') or 0)} of {int(row.get('cwl_hits_expected') or 0)} CWL attacks"
                        if int(row.get("cwl_hits_expected") or 0) > 0
                        else "No CWL attacks during this period"
                    ),
                    "target": (
                        f"Maximum missed attack rate: {fmt_pct(cwl_result.get('allowed_miss_rate_percent') or 0)}"
                        if int(row.get("cwl_hits_expected") or 0) > 0
                        else "Use all CWL attacks"
                    ),
                    "action": per_member_action(cwl_result["hit_usage_verdict"]),
                    "graded": profile_name != "utility",
                },
                "raid_participation": {
                    "name": "Raid Weekend participation",
                    "verdict": raid_result["participation_verdict"],
                    "reason": raid_result["participation_reason"],
                    "current": raid_result["current_participation"],
                    "target": raid_result["target_participation"],
                    "action": per_member_action(raid_result["participation_verdict"]),
                    "graded": profile_name != "utility",
                },
                "raid_value": {
                    "name": "Raid capital gold",
                    "verdict": raid_result["value_verdict"],
                    "reason": raid_result["value_reason"],
                    "current": raid_result["current_value"],
                    "target": raid_result["target_value"],
                    "action": per_member_action(raid_result["value_verdict"]),
                    "graded": profile_name != "utility",
                },
                "clan_games": {
                    "name": "Clan Games",
                    "verdict": cg_result["verdict"],
                    "reason": cg_result["note"],
                    "current": cg_result["current_label"],
                    "target": cg_result["target_label"],
                    "action": per_member_action(cg_result["verdict"]),
                    "graded": profile_name != "utility",
                },
                "donations": {
                    "name": "Donations",
                    "verdict": donation_result["verdict"],
                    "reason": donation_result["note"],
                    "current": donation_result["current_label"],
                    "target": donation_result["target_label"],
                    "action": "Context only",
                    "graded": False,
                    "state": donation_result["state"],
                },
                "progression": {
                    "name": "Progression",
                    "verdict": progression_result["verdict"],
                    "reason": progression_result["reason"],
                    "current": progression_result["detail_label"],
                    "target": "Context only",
                    "action": "Context only",
                    "graded": False,
                    "state": progression_result["state"],
                },
            }

            graded_verdict_map = {
                name: card["verdict"]
                for name, card in signal_cards.items()
                if bool(card.get("graded"))
            }
            overall_details = aggregate_overall_verdict(
                graded_verdict_map,
                profile_name=profile_name,
                return_details=True,
            )
            overall = str(overall_details["overall"])
            soft_count, hard_count = status_counts(graded_verdict_map.values())
            points = soft_count + (hard_count * 2)

            bucket_weights = PROFILE_GRADE_BUCKETS.get(profile_name, PROFILE_GRADE_BUCKETS["casual"])
            signal_bucket_map = _profile_bucket_signals(profile_name)
            signal_weight = {
                signal_name: int(bucket_weights.get(bucket_name, 0))
                for bucket_name, signals in signal_bucket_map.items()
                for signal_name in signals
            }
            flagged_cards = [
                (
                    2 if card["verdict"] == NEEDS_REVIEW else 1,
                    signal_weight.get(signal_key, 0),
                    card["name"],
                    card["reason"],
                )
                for signal_key, card in signal_cards.items()
                if bool(card.get("graded")) and card["verdict"] in {WATCH, NEEDS_REVIEW}
            ]
            flagged_cards.sort(key=lambda item: (-item[0], -item[1], item[2]))
            flagged_cards = [(sev, name, reason) for sev, _, name, reason in flagged_cards]
            row["flags"] = [label for _, label, _ in flagged_cards]
            row["flag_details"] = flagged_cards
            row["priority_score"] = points
            row["soft_miss_count"] = soft_count
            row["hard_miss_count"] = hard_count
            row["status"] = overall
            row["note"] = bullet_lines(detail for _, _, detail in flagged_cards[:3]) if flagged_cards else "No action needed"
            row["signal_cards"] = signal_cards
            row["overall_details"] = overall_details
            row["war_participation_verdict"] = regular_result["verdict"]
            row["war_attendance_verdict"] = regular_result["attendance_verdict"]
            row["war_hit_usage_verdict"] = regular_result["hit_usage_verdict"]
            row["cwl_participation_verdict"] = cwl_result["participation_verdict"]
            row["cwl_hit_usage_verdict"] = cwl_result["hit_usage_verdict"]
            row["raid_participation_verdict"] = raid_result["participation_verdict"]
            row["raid_value_verdict"] = raid_result["value_verdict"]
            row["cg_verdict"] = cg_result["verdict"]
            row["donations_verdict"] = donation_result["verdict"]
            row["progression_verdict"] = progression_result["verdict"]
