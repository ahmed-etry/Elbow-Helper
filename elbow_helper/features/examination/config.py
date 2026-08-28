"""Examination-local configuration."""

from __future__ import annotations

from datetime import timedelta

import discord

from elbow_helper.discord.timezones import build_timezone_select_options

STATE_FILE = "data/examination/examination_state.json"

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
    discord.SelectOption(label="TH11", value="11"),
    discord.SelectOption(label="TH12", value="12"),
    discord.SelectOption(label="TH13", value="13"),
    discord.SelectOption(label="TH14", value="14"),
    discord.SelectOption(label="TH15", value="15"),
    discord.SelectOption(label="TH16", value="16"),
    discord.SelectOption(label="TH17", value="17"),
    discord.SelectOption(label="TH18", value="18"),
]
