"""Shared helper methods for recruitment parsing and formatting."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import discord
from elbow_helper.configuration.clans import CLAN_INFO_BOARDS

from elbow_helper.domain.player_tags import normalize_player_tag

# In-memory rename throttle.
channel_rename_timestamps = {}

def can_rename(guild_id):
    # Soft cap: at most two renamesevery 10 minutes.
    now = datetime.utcnow()
    timestamps = channel_rename_timestamps.get(guild_id, [])
    timestamps = [ts for ts in timestamps if (now - ts).total_seconds() < 600]
    if len(timestamps) >= 2:
        return False
    timestamps.append(now)
    channel_rename_timestamps[guild_id] = timestamps
    return True


def rename_ticket_channel(channel: discord.TextChannel, prefixes: list[tuple[str, str]]) -> Optional[str]:
    """Rename a ticket channel if it matches a known prefix."""
    for old_prefix, new_prefix in prefixes:
        if channel.name.startswith(old_prefix):
            return channel.name.replace(old_prefix, new_prefix, 1)
    return None


class HelperMixin:

    async def _resolve_applicant_id(self, channel: discord.TextChannel) -> Optional[int]:
        """Find the applicant mention from the first few messages in the ticket."""
        first_msg = None
        try:
            async for msg in channel.history(limit=10, oldest_first=True):
                if msg.mentions:
                    first_msg = msg
                    break
        except (discord.Forbidden, discord.HTTPException):
            return None
        if not first_msg or not first_msg.mentions:
            return None
        return first_msg.mentions[0].id


    def _get_clan_role_mentions(self, member: Optional[discord.Member]) -> str:
        """Return clan role mentions for a member based on clan board metadata."""
        if not member:
            return "Unknown"
        clan_role_ids = {
            config.get("clan_role")
            for config in CLAN_INFO_BOARDS.values()
            if config.get("clan_role")
        }
        roles = [role.mention for role in member.roles if role.id in clan_role_ids]
        return ", ".join(roles) if roles else "None"


    @staticmethod
    def _mention_with_nickname(member: Optional[discord.Member], user_id: int) -> str:
        """Format a user as mention + server nickname when available."""
        if not member:
            return f"<@{user_id}>"
        nickname = member.nick or member.name
        return f"{member.mention} ({nickname})"


    def validate_clan_info_boards(self):
        """Validate static clan board metadata configuration."""
        required_keys = {"channel_id", "link", "clan_role"}
        for clan_code, config in CLAN_INFO_BOARDS.items():
            if not isinstance(config, dict):
                self.logger.warning("Invalid clan config for %s: expected dict, got %s", clan_code, type(config).__name__)
                continue

            missing = required_keys - set(config.keys())
            if missing:
                self.logger.warning("Clan config for %s is missing keys: %s", clan_code, ", ".join(sorted(missing)))

            channel_id = config.get("channel_id")
            if not isinstance(channel_id, int):
                self.logger.warning("Clan config for %s has non-integer channel_id: %r", clan_code, channel_id)

            link = config.get("link")
            if not isinstance(link, str) or not link.startswith("http"):
                self.logger.warning("Clan config for %s has invalid link: %r", clan_code, link)


    def parse_clan_input(self, clan_input: str) -> tuple[list[str], list[str]]:
        """Parse clan codes from comma, semicolon, newline, or space-separated input."""
        if not clan_input:
            return [], []

        normalized = clan_input.replace(",", " ").replace(";", " ").replace("\n", " ")
        clans = [clan.strip().upper() for clan in normalized.split(" ")]

        clans = [clan for clan in clans if clan]

        seen = set()
        unique_clans = []
        for clan in clans:
            if clan not in seen:
                seen.add(clan)
                unique_clans.append(clan)

        available_clans = list(CLAN_INFO_BOARDS.keys())
        valid_clans = [clan for clan in unique_clans if clan in available_clans]
        invalid_clans = [clan for clan in unique_clans if clan not in available_clans]

        return valid_clans, invalid_clans

    def parse_player_tag_input(self, raw_input: str) -> tuple[list[str], list[str]]:
        """Parse player tags from comma, semicolon, newline, or space-separated input."""
        if not raw_input:
            return [], []

        normalized = raw_input.replace(",", " ").replace(";", " ").replace("\n", " ")
        values = [value.strip() for value in normalized.split(" ") if value.strip()]

        valid: list[str] = []
        invalid: list[str] = []
        seen: set[str] = set()
        for value in values:
            tag = normalize_player_tag(value)
            if not tag:
                invalid.append(value.strip())
                continue
            if tag in seen:
                continue
            seen.add(tag)
            valid.append(tag)

        return valid, invalid


