"""Shared CWL utility helpers."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Optional

from discord.ext import commands
timezone = dt_timezone


async def wait_for_boot_complete(bot: commands.Bot) -> None:
    await bot.wait_until_ready()
    boot_event = getattr(bot, "boot_complete", None)
    if isinstance(boot_event, asyncio.Event):
        await boot_event.wait()


def coc_time_to_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y%m%dT%H%M%S.%fZ", "%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
_CWL_SEASON_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})(?:-(?P<label>[a-z0-9][a-z0-9-]*))?$"
)


def format_cwl_season_label(value: str) -> str:
    text = str(value or "").strip()
    match = _CWL_SEASON_RE.fullmatch(text)
    if not match:
        return text or "-"
    month = int(match.group("month"))
    if month < 1 or month > 12:
        return text
    label = datetime(int(match.group("year")), month, 1, tzinfo=timezone.utc).strftime("%b %Y")
    suffix = match.group("label")
    return f"{label} ({suffix})" if suffix else label
