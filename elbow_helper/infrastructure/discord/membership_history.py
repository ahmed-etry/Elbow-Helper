"""Text shown for a member's history: time spans and saved Clans, Town Halls and Ranks."""

from __future__ import annotations

import discord

from elbow_helper.configuration.clans import CLAN_CWL_HELPER_ROLE_IDS, CLAN_MEMBER_ROLE_IDS, CLAN_NAMES
from elbow_helper.configuration.roles import LEAD
from elbow_helper.infrastructure.discord.application_emojis import get_application_emoji_provider


def duration(seconds: float) -> str:
    """A length of time in its two largest units, counting 30-day months."""
    days = int(seconds // 86400)
    months, days = divmod(days, 30)
    years, months = divmod(months, 12)
    units = ((years, "year"), (months, "month"), (days, "day"))
    named = [f"{n} {unit}{'' if n == 1 else 's'}" for n, unit in units if n]
    return ", ".join(named[:2]) or "Less than a day"


def _lines(entries: list[str], per_line: int) -> str:
    rows = (" ".join(entries[i:i + per_line]) for i in range(0, len(entries), per_line))
    return "\n".join(rows) or "*None*"


def _section(label: str, entries: list[str], per_line: int) -> tuple[str, str]:
    return (label if len(entries) > 1 else label[:-1]), _lines(entries, per_line)


def saved_clan_and_rank_ids(
    guild: discord.Guild,
    stored_role_ids: list[int],
    snapshot_role_ids: list[int],
) -> tuple[list[int], list[int]]:
    """The saved clan and rank roles that still exist in the server."""
    def existing(role_ids: list[int]) -> list[int]:
        return [role_id for role_id in role_ids if guild.get_role(role_id) is not None]

    clan_role_ids = {
        role_id for code, role_id in CLAN_MEMBER_ROLE_IDS.items()
        if code in CLAN_NAMES and code != "BEW"
    }
    clans = existing([role_id for role_id in stored_role_ids if role_id in clan_role_ids])
    ranks = existing(
        [role_id for role_id in stored_role_ids if role_id in LEAD]
        + [role_id for role_id in snapshot_role_ids
           if role_id not in CLAN_CWL_HELPER_ROLE_IDS.values()]
    )
    return clans, ranks


async def membership_history_fields(
    client: discord.Client,
    guild: discord.Guild,
    stored_role_ids: list[int],
    snapshot_role_ids: list[int],
) -> list[tuple[str, str]]:
    clan_ids, rank_ids = saved_clan_and_rank_ids(guild, stored_role_ids, snapshot_role_ids)
    clans = [f"<@&{role_id}>" for role_id in clan_ids]
    ranks = [f"<@&{role_id}>" for role_id in rank_ids]
    townhall_roles = [
        role for role in sorted(guild.roles, reverse=True)
        if role.id in stored_role_ids and role.name.startswith("TH")
    ]

    catalog = await get_application_emoji_provider(client).get(
        required_names=[role.name for role in townhall_roles],
    )
    townhalls = []
    for role in townhall_roles:
        emoji = catalog.get(role.name)
        townhalls.append(f"{emoji} {role.name}" if emoji else role.name)

    return [
        _section("Clans", clans, 3),
        _section("Town Halls", townhalls, 3),
        _section("Ranks", ranks, 3),
    ]
