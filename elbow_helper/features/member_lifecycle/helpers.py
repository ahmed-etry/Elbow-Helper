from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import discord

from .config import HOME_CLAN_ROLE_IDS, INVITER_TO_PLATFORM

LOGGER = logging.getLogger(__name__)


def platform_from_inviter(name: str | None) -> str:
    if not name:
        return "Referral"
    try:
        cleaned = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", name).strip()
        for candidate in (name, cleaned):
            if not candidate:
                continue
            if candidate in INVITER_TO_PLATFORM:
                return INVITER_TO_PLATFORM[candidate]
            lowered = candidate.lower()
            for key, value in INVITER_TO_PLATFORM.items():
                if key.lower() == lowered:
                    return value
        return "Referral"
    except (TypeError, re.error):
        return "Unknown"


def find_home_clan_name(member: discord.Member) -> str | None:
    for clan_name, role_id in HOME_CLAN_ROLE_IDS.items():
        role = member.guild.get_role(role_id)
        if role and role in member.roles:
            return clan_name
    return None


def roles_intersection(member: discord.Member, id_map: dict[str, int]) -> list[str]:
    names: list[str] = []
    for name, role_id in id_map.items():
        role = member.guild.get_role(role_id)
        if role and role in member.roles:
            names.append(name)
    return names


def human_timedelta(dt_from: datetime, dt_to: datetime | None = None) -> str:
    dt_to = dt_to or datetime.now(timezone.utc)
    delta = dt_to - dt_from
    days = delta.days
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    bits: list[str] = []
    if days:
        bits.append(f"{days}d")
    if hours:
        bits.append(f"{hours}h")
    if minutes and not days:
        bits.append(f"{minutes}m")
    return " ".join(bits) or "0m"


def account_age_str(user: discord.abc.User) -> tuple[str, int]:
    created_at = user.created_at.replace(tzinfo=timezone.utc)
    days_old = (datetime.now(timezone.utc) - created_at).days
    day_word = "day" if days_old == 1 else "days"
    return f"{days_old} {day_word}", days_old


async def snapshot_invites(guild: discord.Guild) -> dict[str, dict[str, Any]]:
    snap: dict[str, dict[str, Any]] = {}
    try:
        invites = await guild.invites()
        for invite in invites:
            snap[invite.code] = {
                "uses": invite.uses or 0,
                "inviter": getattr(invite.inviter, "display_name", None),
            }
    except (discord.Forbidden, discord.HTTPException) as exc:
        LOGGER.warning("Failed to fetch invites: %s", exc)

    try:
        vanity = await guild.vanity_invite()
        if vanity:
            snap[f"vanity:{vanity.code}"] = {"uses": vanity.uses or 0, "inviter": None}
    except (discord.Forbidden, discord.HTTPException):
        LOGGER.debug("Vanity invite unavailable")

    return snap
