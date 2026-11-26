from __future__ import annotations

import calendar
import logging
import re
import textwrap
from datetime import datetime
from collections.abc import Iterable
from typing import Any

import discord

from elbow_helper.configuration.recruitment import INVITER_TO_PLATFORM, REFERRAL_PLATFORM

from .config import VISIBLE_LINE_CHARS

LOGGER = logging.getLogger(__name__)


def platform_from_inviter(name: str | None) -> str:
    if not name:
        return REFERRAL_PLATFORM
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
        return REFERRAL_PLATFORM
    except (TypeError, re.error):
        return REFERRAL_PLATFORM


def role_lines(roles: Iterable[discord.Role]) -> str:
    """Role mentions wrapped by what members see: "@" and the role's name, one space between."""
    lines: list[list[str]] = []
    width = 0
    for role in roles:
        size = len(role.name) + 1
        if lines and width + 1 + size <= VISIBLE_LINE_CHARS:
            lines[-1].append(f"<@&{role.id}>")
            width += 1 + size
        else:
            lines.append([f"<@&{role.id}>"])
            width = size
    return "\n".join(" ".join(line) for line in lines)


def reason_value(emoji: str, text: str, blank_emoji: str | None) -> str:
    """The reason wrapped between words, with continuation lines aligned under the first."""
    lines = textwrap.wrap(text, VISIBLE_LINE_CHARS, break_long_words=False, break_on_hyphens=False)
    new_line = f"\n{blank_emoji} " if blank_emoji else "\n"
    return f"{emoji} {new_line.join(lines)}"


def ticket_log_value(links: list[str], blank_emoji: str | None) -> str:
    """As many whole ticket-log links as one field holds, three to a line."""
    superscripts = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
    items = [
        f"[Jump!{str(index).translate(superscripts) if len(links) > 1 else ''}]({url})"
        for index, url in enumerate(links, start=1)
    ]
    new_line = f"\n{blank_emoji} " if blank_emoji else "\n"
    for shown in range(len(items), -1, -1):
        rows = [" ".join(items[i:min(i + 3, shown)]) for i in range(0, shown, 3)]
        extra = len(items) - shown
        if extra:
            rows.append(f"+{extra} more ticket log{'' if extra == 1 else 's'}")
        value = "📁 " + new_line.join(rows)
        if len(value) <= 1024:
            break
    return value


def _add_months(moment: datetime, months: int) -> datetime:
    index = moment.year * 12 + moment.month - 1 + months
    year, month = divmod(index, 12)
    day = min(moment.day, calendar.monthrange(year, month + 1)[1])
    return moment.replace(year=year, month=month + 1, day=day)


def account_age(created_at: datetime, now: datetime) -> str:
    """How old an account is, in the two largest units, e.g. "3 years, 2 months"."""
    months = (now.year - created_at.year) * 12 + now.month - created_at.month
    if _add_months(created_at, months) > now:
        months -= 1
    days = (now - _add_months(created_at, months)).days
    years, months = divmod(months, 12)
    units = [(years, "year"), (months, "month"), (days, "day")]
    named = [f"{n} {unit}{'' if n == 1 else 's'}" for n, unit in units if n]
    if years:
        return ", ".join(named[:2]) if months else named[0]
    return ", ".join(named[:2]) or "Under a day"


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
