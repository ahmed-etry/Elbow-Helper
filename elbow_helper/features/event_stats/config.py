"""Event stats config, presets, and UI sizing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from elbow_helper.configuration.roles import APPLICANT_ROLE_ID
from elbow_helper.configuration.roles import MEMBER_ROLE_ID
from elbow_helper.configuration.roles import TRIAL_ROLE_ID

from .timeutils import clangames_window
from .timeutils import cwl_window
from .timeutils import league_reset_point
from .timeutils import raid_weekend_window
from .timeutils import season_end_point
from .timeutils import trader_refresh_point

STATE_FILE = Path("data/event_stats/event_stats.json")
STATE_SCHEMA_VERSION = 2
ENDING_SOON_HOURS = 12
DEFAULT_GRACE_HOURS = 24
MAX_GRACE_HOURS = 168
MAX_EVENT_NAME_LENGTH = 60
MAX_CHANNEL_NAME_LENGTH = 100
REFRESH_INTERVAL_SECONDS = 300
HIGH_PRECISION_REFRESH_INTERVAL_SECONDS = 60
HIGH_PRECISION_THRESHOLD_HOURS = 24
COUNTDOWN_MINUTE_INCREMENT = 10
EVENT_SELECTOR_PAGE_SIZE = 20
EVENT_LIST_PAGE_SIZE = 8

RANGE_FUNCTIONS = {
    "cwl": cwl_window,
    "clan_games": clangames_window,
    "raid_weekend": raid_weekend_window,
}

POINT_FUNCTIONS = {
    "league_reset": league_reset_point,
    "season_end": season_end_point,
    "trader_refresh": trader_refresh_point,
}


def build_preset_definitions() -> list[dict[str, Any]]:
    return [
        {
            "key": "members",
            "name": "Members",
            "type": "counter",
            "roles_to_count": [MEMBER_ROLE_ID, TRIAL_ROLE_ID],
            "grace_period_hours": 0,
        },
        {
            "key": "applicants",
            "name": "Applicants",
            "type": "counter",
            "roles_to_count": [APPLICANT_ROLE_ID],
            "grace_period_hours": 0,
        },
        {
            "key": "cwl",
            "name": "CWL",
            "type": "recurring",
            "schedule_shape": "range",
            "schedule_name": "cwl",
            "grace_period_hours": 24,
        },
        {
            "key": "clan_games",
            "name": "Clan Games",
            "type": "recurring",
            "schedule_shape": "range",
            "schedule_name": "clan_games",
            "grace_period_hours": 24,
        },
        {
            "key": "raid_weekend",
            "name": "Raid Weekend",
            "type": "recurring",
            "schedule_shape": "range",
            "schedule_name": "raid_weekend",
            "grace_period_hours": 12,
        },
        {
            "key": "league_reset",
            "name": "League Reset",
            "type": "recurring",
            "schedule_shape": "point",
            "schedule_name": "league_reset",
            "grace_period_hours": 0,
        },
        {
            "key": "season_end",
            "name": "Season End",
            "type": "recurring",
            "schedule_shape": "point",
            "schedule_name": "season_end",
            "grace_period_hours": 0,
        },
        {
            "key": "trader_refresh",
            "name": "Trader Update",
            "type": "recurring",
            "schedule_shape": "point",
            "schedule_name": "trader_refresh",
            "grace_period_hours": 0,
        },
    ]


def build_default_state_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for position, preset in enumerate(build_preset_definitions()):
        events.append(
            {
                "key": preset["key"],
                "source": "preset",
                "enabled": True,
                "name": preset["name"],
                "grace_period_hours": int(preset["grace_period_hours"]),
                "category_id": None,
                "channel_id": None,
                "position": position,
            }
        )
    return events


def get_preset_definition(key: str) -> dict[str, Any] | None:
    for preset in build_preset_definitions():
        if preset["key"] == key:
            return dict(preset)
    return None
