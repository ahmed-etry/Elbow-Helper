"""Typed meanings for live fields and aggregates over observed battle samples."""

from decimal import Decimal
import math

from elbow_helper.features.ranked_stats.battles import RANKED_MODES
from .battle_logs import select_battles


FIELD_MEANINGS = {
    "player": {
        "clan": {"establishes": "current_clan_membership",
                 "does_not_establish": ["home_clan_assignment", "cwl_assignment",
                                        "cwl_participation", "cwl_league"]},
        "heroes": {"establishes": "current_hero_progression",
                   "does_not_establish": ["attacking_skill", "cwl_eligibility"]},
        "leagueTier": {"establishes": "current_ranked_league",
                       "does_not_establish": ["legacy_league", "cwl_league"]},
        "league": {"establishes": "legacy_trophy_league",
                   "does_not_establish": ["current_ranked_league"]},
        "bestTrophies": {"establishes": "all_time_best_trophies",
                         "does_not_establish": ["current_ranked_league", "current_trophies"]},
        "attackWins": {"establishes": "profile_attack_win_counter",
                       "does_not_establish": ["war_participation", "ranked_activity", "inactivity"]},
        "defenseWins": {"establishes": "profile_defense_win_counter",
                        "does_not_establish": ["war_participation", "inactivity"]},
        "trophies": {"establishes": "current_trophies",
                     "does_not_establish": ["current_ranked_league", "war_participation", "inactivity"]},
        "warStars": {"establishes": "cumulative_war_stars",
                     "does_not_establish": ["recent_war_participation", "recent_inactivity"]},
    },
    "clan": {
        "memberList": {"establishes": "current_in_game_roster",
                       "does_not_establish": ["cwl_roster", "cwl_assignment"],
                       "cohort_rule": "For a CWL-only clan use its season's CWL roster or participants; parked accounts are not that cohort."},
        "warLeague": {"establishes": "clan_cwl_league_label",
                      "does_not_establish": ["member_cwl_participation", "eligibility_standard",
                                             "current_cwl_lineup"]},
        "members": {"establishes": "current_member_count",
                    "does_not_establish": ["capacity", "hard_cap", "available_spaces"]},
        "memberList[].clanRank": {"establishes": "current_roster_rank",
                                  "does_not_establish": ["join_time", "membership_tenure"]},
    },
}

AGGREGATES = ("ranked_attacks", "ranked_defenses", "all_attacks", "all_defenses")


def ratio(numerator, denominator, places=4):
    value = Decimal(str(numerator)) / denominator if denominator else None
    return {"numerator": numerator, "denominator": denominator,
            "value": float(value) if value is not None else None,
            "display": format(value, f".{places}f") if value is not None else "unavailable"}


def metrics(rows):
    stars = [row["stars"] for row in rows
             if type(row.get("stars")) is int and 0 <= row["stars"] <= 3]
    destruction = [Decimal(str(row["destructionPercentage"])) for row in rows
                   if type(row.get("destructionPercentage")) in (int, float)
                   and math.isfinite(row["destructionPercentage"])
                   and 0 <= row["destructionPercentage"] <= 100]
    triples = sum(value == 3 for value in stars)
    return {"battles": len(rows), "stars_total": sum(stars), "triples": triples,
            "stars_denominator": len(stars), "destruction_denominator": len(destruction),
            "destruction_total": float(sum(destruction, Decimal(0))),
            "missing_stars": len(rows) - len(stars),
            "missing_destruction": len(rows) - len(destruction),
            "average_stars": ratio(sum(stars), len(stars)),
            "triple_rate_percent": ratio(100 * triples, len(stars), 2),
            "average_destruction_percent": ratio(float(sum(destruction, Decimal(0))), len(destruction), 2)}


def battle_aggregate(payload, selection, *, after=None, before=None):
    # Aggregate the whole matching rolling sample, independently of drill-down paging.
    selected, coverage = select_battles(
        payload, limit=len(payload.get("items", [])), after=after, before=before,
    )
    ranked = selection.startswith("ranked_")
    attack = selection.endswith("_attacks")
    rows = [row for row in selected["items"]
            if row.get("attack") is attack
            and (not ranked or str(row.get("battleType", "")).upper() in RANKED_MODES)]
    unknown_direction = sum(type(row.get("attack")) is not bool for row in selected["items"])
    unknown_mode = sum(not isinstance(row.get("battleType"), str) or not row["battleType"]
                       for row in selected["items"])
    return {"selection": selection, **metrics(rows), "coverage": {
        **coverage, "scope": "matching_observed_battles", "history_complete": False,
        "excluded_rows": len(selected["items"]) - len(rows),
        "unknown_direction_rows": unknown_direction, "unknown_mode_rows": unknown_mode,
    }}


def cohort_aggregate(items, selection, *, after=None, before=None):
    available = [item["aggregate"] for item in items if item.get("status") == "ok"
                 and "aggregate" in item]
    totals = {field: sum(Decimal(str(row[field])) for row in available) for field in (
        "battles", "stars_total", "triples", "stars_denominator", "destruction_denominator",
        "destruction_total", "missing_stars", "missing_destruction",
    )}
    totals = {key: float(value) if key == "destruction_total" else int(value)
              for key, value in totals.items()}
    return {"selection": selection, **totals,
            "average_stars": ratio(totals["stars_total"], totals["stars_denominator"]),
            "triple_rate_percent": ratio(100 * totals["triples"], totals["stars_denominator"], 2),
            "average_destruction_percent": ratio(totals["destruction_total"],
                                                  totals["destruction_denominator"], 2),
            "coverage": {
                "scope": "matching_observed_battles", "history_complete": False,
                "requested_after": after.isoformat() if after else None,
                "requested_before": before.isoformat() if before else None,
                "before_exclusive": True, "requested_accounts": len(items),
                "available_accounts": len(available),
                "unavailable_tags": [item["tag"] for item in items if item.get("status") != "ok"],
                "accounts": [{"tag": item["tag"], **item["aggregate"]["coverage"]}
                             for item in items if "aggregate" in item],
            }}
