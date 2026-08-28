"""Shared constants and static clan routing"""

from __future__ import annotations

from elbow_helper.configuration.channels import CLAN_LEADERSHIP_CHANNELS
from elbow_helper.configuration.channels import CLAN_WAR_CHANNELS
from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.clans import CLAN_LEADERSHIP_ROLE_IDS
from elbow_helper.configuration.clans import CLAN_NAMES

CACHE_FILE = "data/war/war_cache.json"
NOTICE_TTL = 48 * 60 * 60  # Fallback cleanup when no newer summary replaces it.
WAR_FETCH_RETRIES = 3
WAR_FETCH_BACKOFF_SECONDS = 1.5
WAR_FETCH_WARNING_COOLDOWN_SECONDS = 300.0
PROCESSED_WAR_RETENTION = 1500
WAR_BOARD_CLAN_CODES = ("BEH", "BE4", "BES", "BE1", "BEM", "BEC", "BEE")

# Clan tags keyed by full clan name for existing call sites.
CLAN_TAGS = {
    clan.name: clan.tag
    for clan in CLANS.values()
}


def _build_clan_channels() -> dict[str, dict[str, int]]:
    channels: dict[str, dict[str, int]] = {}
    for code, clan_name in CLAN_NAMES.items():
        entry: dict[str, int] = {}

        clan_war_channel_id = CLAN_WAR_CHANNELS.get(code)
        if clan_war_channel_id is not None:
            entry["clan_war_channel"] = clan_war_channel_id

        leadership_channel_id = CLAN_LEADERSHIP_CHANNELS.get(code)
        if leadership_channel_id is not None:
            entry["leadership_channel"] = leadership_channel_id

        leadership_role_id = CLAN_LEADERSHIP_ROLE_IDS.get(code)
        if leadership_role_id is not None:
            entry["leadership_role"] = leadership_role_id

        channels[clan_name] = entry

    return channels


# Clan mapping: clan-war channel, leadership channel, leadership role.
CLAN_CHANNELS = _build_clan_channels()
