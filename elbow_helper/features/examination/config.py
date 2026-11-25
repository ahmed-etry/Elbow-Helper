"""Examination-local configuration."""

from __future__ import annotations

from datetime import timedelta

import discord

from elbow_helper.domain.town_halls import TOWN_HALL_LEVELS
from elbow_helper.infrastructure.discord.timezones import build_timezone_select_options


RESPONSE_DELAY = timedelta(hours=18)
FALLBACK_DELAY = timedelta(hours=24)
REQUIRED_FIELD_RETRY_DELAY = 60
REQUIRED_FIELD_MAX_RETRIES = 5

ROSTER_PAGE_SIZE = 8

AVAILABILITY_DAY_OPTIONS = [
    discord.SelectOption(label="Mon", value="mon"),
    discord.SelectOption(label="Tue", value="tue"),
    discord.SelectOption(label="Wed", value="wed"),
    discord.SelectOption(label="Thu", value="thu"),
    discord.SelectOption(label="Fri", value="fri"),
    discord.SelectOption(label="Sat", value="sat"),
    discord.SelectOption(label="Sun", value="sun"),
]

TIMEZONE_SELECT_OPTIONS = build_timezone_select_options()[:25]

TH_COVERAGE_OPTIONS = [
    discord.SelectOption(label=f"TH{level}", value=str(level))
    for level in range(11, max(TOWN_HALL_LEVELS) + 1)
]
