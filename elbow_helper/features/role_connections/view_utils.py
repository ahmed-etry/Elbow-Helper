"""Shared embed and pagination helpers for role connection views."""

from __future__ import annotations

import re
from datetime import datetime
from datetime import timezone
from typing import Optional

import discord
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL


def build_embed(title: str, description: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
    return embed


def parse_page_from_footer(embed: Optional[discord.Embed], total_pages: int) -> tuple[int, int]:
    if not embed or not embed.footer or not embed.footer.text:
        return 0, total_pages
    match = re.fullmatch(r"Page (\d+)/(\d+)", embed.footer.text.strip())
    if not match:
        return 0, total_pages
    current = max(1, int(match.group(1))) - 1
    total = max(1, int(match.group(2)))
    return current, total


def role_name(guild: Optional[discord.Guild], role_id: int) -> str:
    if guild is not None:
        role = guild.get_role(role_id)
        if role is not None:
            return role.name
    return f"Role {role_id}"

