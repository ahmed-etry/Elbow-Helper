"""Leadership-facing labels and metadata for clan-health config editing."""

from __future__ import annotations

from typing import Any, Dict

PLAYER_BLOCK_ORDER = ["war", "raids", "clan_games"]

PLAYER_LABELS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "war": {
        "_title": "War settings",
        "_help": "Set how many wars members should join and the maximum missed attack rate.",
        "wars_to_join": {
            "label": "Wars to join",
            "help": "This 30-day expectation adjusts to match the selected report period.",
            "unit": "wars",
            "type": "int",
            "min": 0,
            "max": 14,
        },
        "missed_attack_rate_percent": {
            "label": "Maximum missed attack rate",
            "help": "The highest percentage of war and CWL attacks a member can miss before they need review.",
            "unit": "%",
            "type": "int",
            "min": 0,
            "max": 100,
        },
    },
    "raids": {
        "_title": "Raid Weekend",
        "_help": "Members are expected to join every Raid Weekend. Set the minimum capital gold expected when they participate.",
        "minimum_capital_gold_per_event": {
            "label": "Minimum capital gold per Raid Weekend",
            "help": "This applies to each Raid Weekend, not the entire report period.",
            "unit": "gold",
            "type": "int",
            "min": 0,
        },
    },
    "clan_games": {
        "_title": "Clan Games",
        "_help": "Set the minimum Clan Games points expected in the event.",
        "minimum_points_per_event": {
            "label": "Minimum Clan Games points",
            "help": "This requirement appears in health reports and affects the result.",
            "unit": "points",
            "type": "int",
            "min": 0,
            "max": 5000,
        },
    },
}

PROFILE_JUDGMENT_TEXT: Dict[str, tuple[str, ...]] = {
    "competitive": (
        "These settings define what members are expected to do.",
    ),
    "casual": (
        "These settings define what members are expected to do.",
    ),
    "starter": (
        "These settings define what members are expected to do.",
    ),
}
