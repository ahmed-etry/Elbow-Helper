
"""Clan-health local configuration and scoring profiles."""

from __future__ import annotations

import re
from datetime import timezone
from pathlib import Path

from discord import app_commands

from elbow_helper.configuration.clans import CLANS, CLAN_ORDER

UTC = timezone.utc
DB_PATH = Path("data/clan_health/clan_health.db")
SNAPSHOT_LOG_MINUTES = 60

UTILITY_CLANS = {code for code, clan in CLANS.items() if clan.is_utility}
CWL_CLAN_CODES = tuple(code for code in CLAN_ORDER if CLANS[code].cwl_role_id is not None)
CLAN_EXPORT_ORDER = tuple(code for code in CLAN_ORDER if code not in UTILITY_CLANS)
CLAN_CHOICES = [app_commands.Choice(name=code, value=code) for code in (["ALL"] + list(CLAN_EXPORT_ORDER))]

CLAN_PROFILE_BY_CODE: dict[str, str] = {
    "BEH": "competitive",
    "BE4": "competitive",
    "BES": "competitive",
    "BEW": "utility",
    "BE1": "casual",
    "BEM": "casual",
    "BEE": "starter",
    "BEC": "starter",
    "BEP": "utility",
}

PROFILE_NAMES_ORDERED: tuple[str, ...] = ("competitive", "casual", "starter", "utility")
PROFILE_NAMES: frozenset[str] = frozenset(PROFILE_NAMES_ORDERED)

SEASON_KEY_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})$")
