"""Shared verdict and reason helpers for clan health."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from ..models import CLAN_HEALTHY
from ..models import GOOD
from ..models import INSUFFICIENT_DATA
from ..models import NEEDS_REVIEW
from ..models import NOT_TRACKED
from ..models import WATCH
from ..models import normalize_player_verdict
from ..player_health_config import PROFILE_GRADE_BUCKETS, PROFILE_RAID_SERIOUS_MISS_30D

PLAYER_VERDICT_SEVERITY = {
    GOOD: 0,
    WATCH: 1,
    NEEDS_REVIEW: 2,
    INSUFFICIENT_DATA: 0,
    NOT_TRACKED: 0,
}

_LOW_DONATION_THRESHOLD_CLANS = {"BEC", "BEE"}


def normalize_clan_verdict(verdict: Any) -> str:
    text = normalize_player_verdict(verdict)
    if text == GOOD:
        return CLAN_HEALTHY
    return text


def fmt_pts(value: Any, *, signed: bool = False) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "0"
    if signed:
        return f"{number:+,}"
    return f"{number:,}"


def fmt_pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0%"
    rounded = round(number, 1)
    if rounded.is_integer():
        return f"{int(rounded)}%"
    return f"{rounded:.1f}%"


def assess_missed_attack_rate(*, expected: int, used: int, allowed_rate_percent: float) -> Dict[str, Any]:
    expected_hits = max(0, int(expected or 0))
    used_hits = max(0, int(used or 0))
    missed_hits = max(0, expected_hits - used_hits)
    if expected_hits <= 0:
        return {
            "verdict": INSUFFICIENT_DATA,
            "reason": "Not enough war attack history is available for this period.",
            "missed_hits": missed_hits,
            "missed_rate_percent": None,
            "allowed_rate_percent": float(allowed_rate_percent),
            "watch_rate_percent": min(100.0, float(allowed_rate_percent) + 10.0),
        }

    allowed_rate = max(0.0, min(100.0, float(allowed_rate_percent)))
    missed_rate = (missed_hits / expected_hits) * 100.0
    watch_rate = min(100.0, allowed_rate + 10.0)
    if missed_rate <= allowed_rate:
        verdict = GOOD
    elif missed_rate <= watch_rate:
        verdict = WATCH
    else:
        verdict = NEEDS_REVIEW
    reason = (
        f"Missed {missed_hits} of {expected_hits} war attacks "
        f"({fmt_pct(missed_rate)} missed); allowed {fmt_pct(allowed_rate)}"
    )
    return {
        "verdict": verdict,
        "reason": reason,
        "missed_hits": missed_hits,
        "missed_rate_percent": missed_rate,
        "allowed_rate_percent": allowed_rate,
        "watch_rate_percent": watch_rate,
    }


def bullet_lines(lines: Iterable[str]) -> str:
    cleaned = [str(line).strip() for line in lines if str(line or "").strip()]
    if not cleaned:
        return "No action needed"
    return "\n".join(f"- {line}" for line in cleaned)


def per_member_action(verdict: str) -> str:
    canonical = normalize_player_verdict(verdict)
    if canonical == GOOD:
        return "No action needed"
    if canonical == WATCH:
        return "Monitor next window"
    if canonical == NEEDS_REVIEW:
        return "Review soon"
    if canonical == INSUFFICIENT_DATA:
        return "Check again when more activity is available"
    return "-"


def clan_action_summary(*, needs_review_names: Sequence[str], watch_count: int) -> str:
    names = [str(name).strip() for name in needs_review_names if str(name or "").strip()]
    if not names and watch_count <= 0:
        return "No action needed."
    if not names:
        return "Check again after the next reporting period."
    if len(names) <= 3:
        return "Review soon: " + ", ".join(names) + "."
    return f"Leadership follow-up: {len(names)} members need review."


def worst_player_verdict(
    verdicts: Iterable[str],
    *,
    ignore: Optional[Set[str]] = None,
    default: str = INSUFFICIENT_DATA,
) -> str:
    ignore_set = {normalize_player_verdict(value) for value in (ignore or set())}
    relevant = [
        normalize_player_verdict(verdict)
        for verdict in verdicts
        if normalize_player_verdict(verdict) not in ignore_set
    ]
    if not relevant:
        return default
    return max(relevant, key=lambda value: PLAYER_VERDICT_SEVERITY.get(value, 0))


def status_counts(per_signal_verdicts: Iterable[str]) -> tuple[int, int]:
    relevant = [normalize_player_verdict(v) for v in per_signal_verdicts if str(v or "").strip()]
    soft = sum(1 for value in relevant if PLAYER_VERDICT_SEVERITY.get(value, 0) == 1)
    hard = sum(1 for value in relevant if PLAYER_VERDICT_SEVERITY.get(value, 0) >= 2)
    return soft, hard


def _profile_bucket_signals(profile_name: str) -> Dict[str, List[str]]:
    profile = str(profile_name or "casual").strip().lower()
    if profile == "competitive":
        return {
            "war": ["war_attendance", "war_hit_usage", "cwl_participation", "cwl_hit_usage"],
            "raids": ["raid_participation", "raid_value"],
            "clan_games": ["clan_games"],
        }
    if profile in {"casual", "starter"}:
        return {
            "cwl": ["cwl_participation", "cwl_hit_usage"],
            "raids": ["raid_participation", "raid_value"],
            "clan_games": ["clan_games"],
        }
    return {}


def _verdict_score(verdict: str) -> Optional[float]:
    canonical = normalize_player_verdict(verdict)
    if canonical == GOOD:
        return 1.0
    if canonical == WATCH:
        return 0.5
    if canonical == NEEDS_REVIEW:
        return 0.0
    return None


def aggregate_overall_verdict(
    per_signal_verdicts: Iterable[str] | Dict[str, str],
    *,
    profile_name: str = "casual",
    return_details: bool = False,
) -> str | Dict[str, Any]:
    if isinstance(per_signal_verdicts, dict):
        verdict_map = {name: normalize_player_verdict(verdict) for name, verdict in per_signal_verdicts.items()}
    else:
        verdict_map = {str(index): normalize_player_verdict(verdict) for index, verdict in enumerate(per_signal_verdicts)}

    profile = str(profile_name or "casual").strip().lower()
    bucket_weights = PROFILE_GRADE_BUCKETS.get(profile, PROFILE_GRADE_BUCKETS["casual"])
    bucket_signals = _profile_bucket_signals(profile)

    available_weight = 0
    weighted_score = 0.0
    bucket_details: List[str] = []
    bucket_verdicts: Dict[str, str] = {}
    bucket_scores: Dict[str, float] = {}

    for bucket_name, weight in bucket_weights.items():
        signal_names = bucket_signals.get(bucket_name, [])
        verdicts = [
            verdict_map[name]
            for name in signal_names
            if name in verdict_map and verdict_map[name] not in {NOT_TRACKED, INSUFFICIENT_DATA}
        ]
        if not verdicts:
            bucket_verdicts[bucket_name] = INSUFFICIENT_DATA
            bucket_details.append(f"{bucket_name}: {INSUFFICIENT_DATA}")
            continue
        scores = [_verdict_score(verdict) for verdict in verdicts]
        numeric_scores = [score for score in scores if score is not None]
        if not numeric_scores:
            bucket_verdicts[bucket_name] = INSUFFICIENT_DATA
            bucket_details.append(f"{bucket_name}: {INSUFFICIENT_DATA}")
            continue
        bucket_score = sum(numeric_scores) / len(numeric_scores)
        bucket_verdict = worst_player_verdict(verdicts, ignore={NOT_TRACKED, INSUFFICIENT_DATA}, default=INSUFFICIENT_DATA)
        bucket_verdicts[bucket_name] = bucket_verdict
        bucket_scores[bucket_name] = round(bucket_score * 100.0, 1)
        available_weight += int(weight)
        weighted_score += bucket_score * float(weight)
        bucket_details.append(f"{bucket_name}: {bucket_verdict} ({bucket_scores[bucket_name]:.0f}/100)")

    if available_weight < 50:
        overall = INSUFFICIENT_DATA
        normalized_score = None
    else:
        normalized_score = round((weighted_score / available_weight) * 100.0, 1)
        if normalized_score >= 85.0:
            overall = GOOD
        elif normalized_score >= 60.0:
            overall = WATCH
        else:
            overall = NEEDS_REVIEW

        dominant_bucket_nr = any(
            bucket_verdicts.get(name) == NEEDS_REVIEW
            and int(bucket_weights.get(name, 0)) >= 50
            for name in bucket_verdicts
        )
        if dominant_bucket_nr and overall == GOOD:
            overall = WATCH

    details = {
        "overall": overall,
        "weighted_sum": normalized_score,
        "needs_review_signals": [name for name, verdict in verdict_map.items() if verdict == NEEDS_REVIEW],
        "insufficient_signals": [name for name, verdict in verdict_map.items() if verdict == INSUFFICIENT_DATA],
        "graded_signals": [name for name, verdict in verdict_map.items() if verdict not in {NOT_TRACKED, INSUFFICIENT_DATA}],
        "breakdown": bucket_details,
        "bucket_verdicts": bucket_verdicts,
        "bucket_scores": bucket_scores,
    }
    return details if return_details else overall


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scaled_base_window_target(*, base_target: int, cycle_start: Optional[datetime], cycle_end: Optional[datetime]) -> int:
    if base_target <= 0:
        return 0
    if cycle_start is None or cycle_end is None:
        return base_target
    window_days = max(1, int((cycle_end - cycle_start).days))
    return max(1, round(base_target * window_days / 30))


def assess_war_participation(
    *,
    profile_rules: Dict[str, Any],
    cycle_start: Optional[datetime],
    cycle_end: Optional[datetime],
    war_events_joined: int,
    war_expected: int,
    war_used: int,
    available_wars_in_window: Optional[int],
) -> Dict[str, Any]:
    war_rules = profile_rules.get("war", {}) if isinstance(profile_rules, dict) else {}
    base_wars_required = max(0, _coerce_int(war_rules.get("wars_to_join")) or 0)
    allowed_miss_rate = max(0.0, min(100.0, float(_coerce_int(war_rules.get("missed_attack_rate_percent")) or 0)))
    scaled_required = _scaled_base_window_target(
        base_target=base_wars_required,
        cycle_start=cycle_start,
        cycle_end=cycle_end,
    )
    available_wars = max(0, int(available_wars_in_window or 0))
    joined = max(0, int(war_events_joined or 0))
    expected = max(0, int(war_expected or 0))
    used = max(0, int(war_used or 0))

    if base_wars_required <= 0:
        attendance_verdict = NOT_TRACKED
        attendance_reason = "Regular wars are not required for this clan."
    elif available_wars <= 0 and joined <= 0:
        attendance_verdict = INSUFFICIENT_DATA
        attendance_reason = "Not enough war participation history is available for this period."
    else:
        required = min(scaled_required, available_wars) if available_wars > 0 else scaled_required
        if joined >= required:
            attendance_verdict = GOOD
        elif joined > 0:
            attendance_verdict = WATCH
        else:
            attendance_verdict = NEEDS_REVIEW
        attendance_reason = f"Joined {joined} of {required} expected wars during this period"

    if expected <= 0:
        hit_usage_verdict = INSUFFICIENT_DATA if attendance_verdict != NOT_TRACKED else NOT_TRACKED
        hit_usage_reason = (
            "War attacks aren't included in this clan's health reports."
            if attendance_verdict == NOT_TRACKED
            else "Not enough war attack history is available for this period."
        )
        missed_hits = 0
        missed_rate_percent = None
    else:
        usage_result = assess_missed_attack_rate(
            expected=expected,
            used=used,
            allowed_rate_percent=allowed_miss_rate,
        )
        hit_usage_verdict = usage_result["verdict"]
        hit_usage_reason = usage_result["reason"]
        missed_hits = int(usage_result["missed_hits"] or 0)
        missed_rate_percent = usage_result["missed_rate_percent"]

    overall = worst_player_verdict(
        [attendance_verdict, hit_usage_verdict],
        ignore={NOT_TRACKED, INSUFFICIENT_DATA},
        default=INSUFFICIENT_DATA if attendance_verdict != NOT_TRACKED else NOT_TRACKED,
    )
    war_word = "war" if joined == 1 else "wars"
    attack_word = "attack" if expected == 1 else "attacks"
    return {
        "verdict": overall,
        "attendance_verdict": attendance_verdict,
        "hit_usage_verdict": hit_usage_verdict,
        "attendance_reason": attendance_reason,
        "hit_usage_reason": hit_usage_reason,
        "current_label": (
            f"Joined {joined} {war_word} | Missed {missed_hits} of {expected} {attack_word}"
            if expected > 0
            else f"Joined {joined} {war_word}"
        ),
        "target_label": (
            f"Wars to join: {scaled_required}; maximum missed attack rate: {fmt_pct(allowed_miss_rate)}"
            if base_wars_required > 0 or expected > 0
            else "Context only"
        ),
        "missed_hits": missed_hits,
        "missed_rate_percent": missed_rate_percent,
        "allowed_missed_attack_rate_percent": allowed_miss_rate,
        "wars_required": scaled_required,
        "base_wars_required": base_wars_required,
        "available_wars": available_wars,
    }


def progression_assessment(
    *,
    th_delta: Any,
    hero_delta: Any,
    pet_delta: Any,
    equipment_delta: Any,
    troop_delta: Any,
    spell_delta: Any,
) -> Dict[str, Any]:
    deltas: Dict[str, Optional[int]] = {
        "th_delta": _coerce_int(th_delta),
        "hero_delta": _coerce_int(hero_delta),
        "pet_delta": _coerce_int(pet_delta),
        "equipment_delta": _coerce_int(equipment_delta),
        "troop_delta": _coerce_int(troop_delta),
        "spell_delta": _coerce_int(spell_delta),
    }
    available_values = [value for value in deltas.values() if value is not None]
    positive_keys = [key for key, value in deltas.items() if value is not None and value > 0]
    detail_parts = []
    for key, label in (
        ("hero_delta", "Heroes"),
        ("th_delta", "TH"),
        ("pet_delta", "Pets"),
        ("equipment_delta", "Gear"),
        ("troop_delta", "Troops"),
        ("spell_delta", "Spells"),
    ):
        value = deltas.get(key)
        if value is not None:
            detail_parts.append(f"{label} {fmt_pts(value, signed=True)}")
    detail_label = ", ".join(detail_parts) if detail_parts else "No progression history for this period"

    if not available_values:
        verdict = INSUFFICIENT_DATA
        reason = "Not enough progression history is available for this period."
        state = "No data"
    elif positive_keys:
        verdict = GOOD
        reason = "Any progression during this period meets the expectation."
        state = "OK"
    else:
        verdict = WATCH
        reason = "No progression during this period."
        state = "No activity"

    return {
        "verdict": verdict,
        "state": state,
        "detail_label": detail_label,
        "reason": reason,
        "has_progression_data": bool(available_values),
        "meaningful_progress": bool(positive_keys),
        "positive_keys": positive_keys,
        "deltas": deltas,
    }


def donation_activity_assessment(
    *,
    clan_code: str,
    donations: Any,
    received: Any,
) -> Dict[str, Any]:
    donated = max(0, _coerce_int(donations) or 0)
    received_total = max(0, _coerce_int(received) or 0)
    code = str(clan_code or "").upper()
    threshold = 100 if code in _LOW_DONATION_THRESHOLD_CLANS else 400

    if donated == 0 and received_total == 0:
        verdict = WATCH
        state = "No donations"
        note = "No troops donated or received."
    elif donated < threshold and received_total < threshold:
        verdict = WATCH
        state = "Low activity"
        note = f"Below the expected donation activity for this clan ({threshold})."
    else:
        verdict = GOOD
        state = "OK"
        note = "Donation activity meets the expectation for this clan."

    return {
        "verdict": verdict,
        "state": state,
        "note": note,
        "evidence_state": "full",
        "donations": donated,
        "received": received_total,
        "threshold": threshold,
        "current_label": f"{donated:,} donated / {received_total:,} received",
        "target_label": "Context only",
    }


def support_assessment(
    *,
    donations: Any,
    received: Any,
    clan_code: str = "",
) -> Dict[str, Any]:
    return donation_activity_assessment(clan_code=clan_code, donations=donations, received=received)


def clan_games_assessment(
    *,
    clan_games_rules: Dict[str, Any],
    cg_delta: Any,
    window_state: str,
) -> Dict[str, Any]:
    delta = _coerce_int(cg_delta)
    target_points = max(0, _coerce_int(clan_games_rules.get("minimum_points_per_event")) or 0)
    if window_state == "N/A":
        verdict = NOT_TRACKED
        note = "Clan Games did not run during this period."
    elif window_state == "Partial":
        verdict = INSUFFICIENT_DATA
        note = "Only part of the Clan Games event falls within this period."
    elif target_points <= 0:
        verdict = NOT_TRACKED
        note = "Clan Games isn't included in this clan's health reports."
    elif delta is None:
        verdict = INSUFFICIENT_DATA
        note = "Not enough Clan Games history is available for this period."
    elif delta >= target_points:
        verdict = GOOD
        note = f"Earned {delta:,} of {target_points:,} Clan Games points"
    elif delta > 0 and delta >= max(1, math.ceil(target_points * 0.5)):
        verdict = WATCH
        note = f"Earned {delta:,} of {target_points:,} Clan Games points"
    else:
        verdict = NEEDS_REVIEW
        note = f"Earned {max(0, delta or 0):,} of {target_points:,} Clan Games points"
    return {
        "verdict": verdict,
        "severity": PLAYER_VERDICT_SEVERITY.get(verdict, 0),
        "current_label": f"{fmt_pts(delta or 0, signed=True)} pts",
        "target_label": f"Minimum Clan Games points: {target_points:,}" if target_points > 0 else "Not tracked",
        "note": note,
        "delta": delta,
        "target_points": target_points,
    }


def _scaled_raid_serious_threshold(*, profile_name: str, weekends_available: int) -> int:
    base = int(PROFILE_RAID_SERIOUS_MISS_30D.get(str(profile_name or "casual").strip().lower(), 3))
    if weekends_available <= 2:
        return max(2, weekends_available)
    return max(2, round(base * (weekends_available / 4.0)))


def raid_assessment(
    *,
    profile_rules: Dict[str, Any],
    raid_enabled: bool,
    raid_used: Any,
    raid_loot: Any,
    window_state: Optional[str] = None,
    weekends_joined: int = 0,
    weekends_available: int = 0,
) -> Dict[str, Any]:
    profile_name = str(profile_rules.get("_profile_name") or "casual").strip().lower()
    raids_block = profile_rules.get("raids", {}) if isinstance(profile_rules, dict) else {}
    minimum_gold = max(0, _coerce_int(raids_block.get("minimum_capital_gold_per_event")) or 0)
    used = max(0, _coerce_int(raid_used) or 0)
    loot = max(0, _coerce_int(raid_loot) or 0)
    joined = max(0, int(weekends_joined or 0))
    available = max(0, int(weekends_available or 0))
    missed_events = max(0, available - joined)
    serious_threshold = _scaled_raid_serious_threshold(profile_name=profile_name, weekends_available=available)

    if not raid_enabled:
        participation_verdict = NOT_TRACKED
        value_verdict = NOT_TRACKED
        participation_reason = "Raid Weekend participation isn't included in this clan's health reports."
        value_reason = "Capital gold isn't included in this clan's health reports."
    elif window_state == "N/A":
        participation_verdict = NOT_TRACKED
        value_verdict = NOT_TRACKED
        participation_reason = "No Raid Weekend occurred during this period."
        value_reason = "No Raid Weekend occurred during this period."
    elif available <= 0:
        participation_verdict = INSUFFICIENT_DATA
        value_verdict = INSUFFICIENT_DATA
        participation_reason = "Not enough Raid Weekend history is available for this period."
        value_reason = "Not enough capital gold history is available for this period."
    else:
        if joined >= available:
            participation_verdict = GOOD
        elif missed_events >= serious_threshold:
            participation_verdict = NEEDS_REVIEW
        elif missed_events >= max(1, serious_threshold - 1) and serious_threshold >= 3:
            participation_verdict = WATCH
        else:
            participation_verdict = GOOD

        if joined >= available:
            participation_reason = f"Joined all {available} Raid Weekends during this period"
        elif joined > 0:
            participation_reason = f"Joined {joined} of {available} Raid Weekends during this period"
        else:
            participation_reason = f"Missed all {available} Raid Weekends during this period"

        if joined <= 0 or used <= 0:
            value_verdict = NOT_TRACKED
            value_reason = "Capital gold is only checked when the player joins a Raid Weekend."
        elif minimum_gold <= 0:
            value_verdict = NOT_TRACKED
            value_reason = "Capital gold isn't included in this clan's health reports."
        else:
            average_gold = loot / max(1, joined)
            if average_gold >= minimum_gold:
                value_verdict = GOOD
                value_reason = f"Averaged {int(average_gold):,} capital gold per Raid Weekend"
            elif average_gold >= max(1, minimum_gold * 0.5):
                value_verdict = WATCH
                value_reason = (
                    f"Averaged {int(average_gold):,} capital gold per Raid Weekend (minimum {minimum_gold:,})"
                )
            else:
                value_verdict = NEEDS_REVIEW
                value_reason = (
                    f"Averaged {int(average_gold):,} capital gold per Raid Weekend (minimum {minimum_gold:,})"
                )

    combined_verdict = worst_player_verdict(
        [participation_verdict, value_verdict],
        ignore={NOT_TRACKED, INSUFFICIENT_DATA},
        default=INSUFFICIENT_DATA,
    )
    return {
        "participation_verdict": participation_verdict,
        "value_verdict": value_verdict,
        "combined_verdict": combined_verdict,
        "participation_severity": PLAYER_VERDICT_SEVERITY.get(participation_verdict, 0),
        "value_severity": PLAYER_VERDICT_SEVERITY.get(value_verdict, 0),
        "current_participation": f"{joined} of {available} Raid Weekends",
        "current_value": (
            f"{loot:,} capital gold across 1 Raid Weekend"
            if joined == 1
            else f"{loot:,} capital gold across {joined} Raid Weekends"
            if joined > 1
            else "No Raid Weekend participation during this period"
        ),
        "target_participation": "Join every Raid Weekend",
        "target_value": f"Minimum capital gold per Raid Weekend: {minimum_gold:,}" if minimum_gold > 0 else "Not tracked",
        "participation_reason": participation_reason,
        "value_reason": value_reason,
        "target_loot_per_attack": minimum_gold,
        "expected": 0,
        "used": used,
        "loot": loot,
        "target_attacks": 0,
        "target_weekends": available,
        "weekends_joined": joined,
        "weekends_available": available,
        "missed_events": missed_events,
        "serious_threshold": serious_threshold,
        "target_weekend_reason": "Join every Raid Weekend",
    }


def trend_from_verdicts(verdicts: Sequence[str]) -> tuple[str, int]:
    cleaned = [normalize_player_verdict(verdict) for verdict in verdicts if str(verdict or "").strip()]
    if len(cleaned) < 3:
        return "->", 0
    severities = [PLAYER_VERDICT_SEVERITY.get(value, 0) for value in cleaned[:3]]
    if severities[0] == severities[1] == severities[2]:
        arrow = "->"
    elif severities[0] < severities[1]:
        arrow = "^"
    elif severities[0] > severities[1]:
        arrow = "v"
    else:
        arrow = "->"
    current = cleaned[0]
    streak = 1
    if current not in {GOOD, INSUFFICIENT_DATA, NOT_TRACKED}:
        for value in cleaned[1:]:
            if value == current:
                streak += 1
            else:
                break
    else:
        streak = 0
    return arrow, streak
