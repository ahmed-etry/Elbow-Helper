"""Roster message rendering adapted to the server's account data."""

from __future__ import annotations

from datetime import datetime

import discord

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from ..config import ROSTER_CLAN_COLUMN_WIDTH
from ..config import ROSTER_EMOJI_ROWS_PER_PAGE
from ..config import ROSTER_ROWS_PER_PAGE
from ..config import FAMILY_DISPLAY_NAME
from .emojis import TownHallEmojiSet
from ..models import Roster
from ..models import RosterLayout
from ..models import RosterMember


def _trim(value: str, width: int) -> str:
    return value if len(value) <= width else f"{value[: width - 1]}…"


def _table(
    members: list[RosterMember],
    display_names: dict[int, str],
    layout: RosterLayout,
    townhall_emojis: TownHallEmojiSet | None,
    *,
    use_townhall_emojis: bool,
) -> str:
    header: list[str] = []
    if layout.show_townhall and not use_townhall_emojis:
        header.append("TH")
    header.append(f"{'PLAYER':<{layout.player_width}}")
    if layout.show_discord:
        header.append(f"{'DISCORD':<{layout.discord_width}}")
    if layout.show_clan:
        header.append(f"{'CLAN':>{ROSTER_CLAN_COLUMN_WIDTH}}")
    header_text = f"`{' '.join(header)}`"
    lines = [
        f"{townhall_emojis.header} {header_text}"
        if use_townhall_emojis and townhall_emojis is not None
        else header_text
    ]
    for member in members:
        cells: list[str] = []
        if layout.show_townhall and not use_townhall_emojis:
            townhall = str(member.townhall) if member.townhall else "-"
            cells.append(f"{townhall:>2}")
        cells.append(
            f"{_trim(member.player_name, layout.player_width):<{layout.player_width}}"
        )
        if layout.show_discord:
            discord_name = display_names.get(member.discord_user_id, str(member.discord_user_id))
            cells.append(
                f"{_trim(discord_name, layout.discord_width):<{layout.discord_width}}"
            )
        if layout.show_clan:
            clan_code = _trim(member.clan_code or "-", ROSTER_CLAN_COLUMN_WIDTH)
            cells.append(f"{clan_code:>{ROSTER_CLAN_COLUMN_WIDTH}}")
        row_text = f"`{' '.join(cells)}`"
        if use_townhall_emojis and townhall_emojis is not None:
            lines.append(f"{townhall_emojis.levels[member.townhall]} {row_text}")
        else:
            lines.append(row_text)
    return "\n".join(lines)


def _can_use_townhall_emojis(
    members: list[RosterMember],
    layout: RosterLayout,
    townhall_emojis: TownHallEmojiSet | None,
) -> bool:
    if not layout.show_townhall or townhall_emojis is None:
        return False
    if townhall_emojis.header is None:
        return False
    return all(member.townhall in townhall_emojis.levels for member in members)


def _status(
    roster: Roster,
    total: int,
    opens_at: datetime | None,
    closes_at: datetime | None,
) -> str:
    lines: list[str] = []
    if roster.role_id:
        lines.append(f"Role <@&{roster.role_id}>")
    minimum = f" | Min. TH{roster.min_townhall}" if roster.min_townhall else ""
    lines.append(f"Total {total}/{roster.max_members}{minimum}")
    if not roster.buttons_hidden:
        if roster.status == "open" and closes_at is not None:
            lines.append(f"Signup closes on {discord.utils.format_dt(closes_at)}")
        elif roster.status != "open" and opens_at is not None:
            lines.append(f"Signup opens on {discord.utils.format_dt(opens_at)}")
        elif roster.status != "open":
            lines.append("Signup is **closed**")
    return "\n".join(lines)


def roster_rows_per_page(
    members: list[RosterMember],
    layout: RosterLayout,
    townhall_emojis: TownHallEmojiSet | None,
) -> int:
    if _can_use_townhall_emojis(members, layout, townhall_emojis):
        return ROSTER_EMOJI_ROWS_PER_PAGE
    return ROSTER_ROWS_PER_PAGE


def roster_page_count(total: int, rows_per_page: int = ROSTER_ROWS_PER_PAGE) -> int:
    return max(1, (total + rows_per_page - 1) // rows_per_page)


def build_roster_embeds(
    roster: Roster,
    members: list[RosterMember],
    display_names: dict[int, str] | None = None,
    closes_at: datetime | None = None,
    *,
    opens_at: datetime | None = None,
    page: int = 0,
    family_icon_url: str | None = None,
    clan_icon_url: str | None = None,
    layout: RosterLayout | None = None,
    townhall_emojis: TownHallEmojiSet | None = None,
    rows_per_page: int | None = None,
) -> list[discord.Embed]:
    names = display_names or {}
    roster_layout = layout or RosterLayout()
    resolved_rows_per_page = rows_per_page or roster_rows_per_page(
        members,
        roster_layout,
        townhall_emojis,
    )
    use_townhall_emojis = _can_use_townhall_emojis(
        members,
        roster_layout,
        townhall_emojis,
    )
    page = min(
        max(page, 0),
        roster_page_count(len(members), resolved_rows_per_page) - 1,
    )
    start = page * resolved_rows_per_page
    page_members = members[start:start + resolved_rows_per_page]
    clan = CLANS.get(roster.clan_code)
    embed = discord.Embed(
        title=roster.name,
        description=_table(
            page_members,
            names,
            roster_layout,
            townhall_emojis,
            use_townhall_emojis=use_townhall_emojis,
        ),
        color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
    )
    if clan:
        embed.set_author(
            name=f"{clan.name} • {clan.tag}",
            url=f"http://cprk.us/c/{clan.tag.lstrip('#')}",
            icon_url=clan_icon_url,
        )
    else:
        embed.set_author(name=FAMILY_DISPLAY_NAME, icon_url=family_icon_url)

    embed.add_field(
        name="\u200e",
        value=_status(roster, len(members), opens_at, closes_at),
        inline=False,
    )
    return [embed]
