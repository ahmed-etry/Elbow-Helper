"""Discord components for selecting supported community timezones."""

from __future__ import annotations

from datetime import datetime

import discord
from discord import app_commands

from elbow_helper.domain.timezones import TIMEZONE_ENTRIES
from elbow_helper.infrastructure.time import format_utc_offset
from elbow_helper.infrastructure.time import resolve_timezone
from elbow_helper.infrastructure.time import utc_now


def build_timezone_select_options(
    at: datetime | None = None,
) -> list[discord.SelectOption]:
    """Build the supported timezone options for Discord select menus."""
    options: list[discord.SelectOption] = []
    for label, zone_name in TIMEZONE_ENTRIES:
        offset = format_utc_offset(zone_name, at) or "UTC+00:00"
        options.append(
            discord.SelectOption(
                label=f"{offset} - {label}",
                value=zone_name,
                description=zone_name,
            )
        )
    return options


def build_timezone_choices(
    current: str,
    at: datetime | None = None,
) -> list[app_commands.Choice[str]]:
    """Build sorted Discord autocomplete choices for supported timezones."""
    current_lower = (current or "").strip().lower()
    now_utc = at or utc_now()

    matches: list[tuple[int, str, str]] = []
    for label, zone_name in TIMEZONE_ENTRIES:
        if (
            current_lower
            and current_lower not in label.lower()
            and current_lower not in zone_name.lower()
        ):
            continue

        zone = resolve_timezone(zone_name)
        if zone is None:
            continue

        local_time = now_utc.astimezone(zone)
        minute_of_day = local_time.hour * 60 + local_time.minute
        label_time = f"{local_time:%H:%M} - {label}"
        matches.append((minute_of_day, label_time, zone_name))

    matches.sort(key=lambda item: (item[0], item[1]))
    choices = [
        app_commands.Choice(name=label_time, value=zone_name)
        for _, label_time, zone_name in matches[:25]
    ]
    if choices:
        return choices

    return [
        app_commands.Choice(
            name=f"{now_utc:%H:%M} - UTC",
            value="UTC",
        )
    ]
