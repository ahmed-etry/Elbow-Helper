"""Typed meanings for raw live Clash fields."""

from ..reads.ranked_measures import ratio as ratio

__all__ = ['FIELD_MEANINGS', 'ratio']
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
