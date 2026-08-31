"""Rendering for the persistent regular-war overview."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from datetime import timezone
from typing import Any
from urllib.parse import quote

import discord

from .emojis import WarEmojiSet


WAR_STATE_COLORS = {
    "preparation": 16745216,
    "inwar": 16345172,
}
WAR_RESULT_COLORS = {
    "won": 3066993,
    "lost": 15158332,
    "tied": 5861569,
}


def normalize_war_state(value: object) -> str:
    return str(value or "").replace("_", "").casefold()


def _clan_url(tag: object) -> str:
    return (
        "https://link.clashofclans.com/en?action=OpenClanProfile&tag="
        f"{quote(str(tag or ''), safe='')}"
    )


def _badge_url(clan: dict[str, Any]) -> str | None:
    badges = clan.get("badgeUrls") or {}
    return badges.get("small") or badges.get("medium") or badges.get("large")


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _attacks(clan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        attack
        for member in clan.get("members") or []
        for attack in member.get("attacks") or []
        if isinstance(attack, dict)
    ]


def _attack_count(clan: dict[str, Any]) -> int:
    return _as_int(clan.get("attacks"), len(_attacks(clan)))


def _war_result(clan: dict[str, Any], opponent: dict[str, Any]) -> str:
    clan_score = (
        _as_int(clan.get("stars")),
        _as_float(clan.get("destructionPercentage")),
    )
    opponent_score = (
        _as_int(opponent.get("stars")),
        _as_float(opponent.get("destructionPercentage")),
    )
    if clan_score > opponent_score:
        return "won"
    if clan_score < opponent_score:
        return "lost"
    return "tied"


def _war_stats(
    data: dict[str, Any],
    clan: dict[str, Any],
    opponent: dict[str, Any],
    emojis: WarEmojiSet,
) -> str:
    star = emojis.icon("yellow_star", "⭐")
    sword = emojis.icon("sword", "⚔️")
    fire = emojis.icon("fire", "🔥")
    clan_stars = str(_as_int(clan.get("stars")))
    opponent_stars = str(_as_int(opponent.get("stars")))
    total_attacks = (
        _as_int(data.get("teamSize"))
        * _as_int(data.get("attacksPerMember"), 2)
    )
    clan_attacks = f"{max(0, total_attacks - _attack_count(clan))} left"
    opponent_attacks = f"{max(0, total_attacks - _attack_count(opponent))} left"
    clan_destruction = f"{_as_float(clan.get('destructionPercentage')):.2f}%"
    opponent_destruction = f"{_as_float(opponent.get('destructionPercentage')):.2f}%"
    return "\n".join(
        (
            f"`\u200e{clan_stars:>8} \u200f`\u200e \u2002 {star} \u2002 "
            f"`\u200e {opponent_stars:<8}\u200f`",
            f"`\u200e{clan_attacks:>8} \u200f`\u200e \u2002 {sword} \u2002 "
            f"`\u200e {opponent_attacks:<8}\u200f`",
            f"`\u200e{clan_destruction:>8} \u200f`\u200e \u2002 {fire} \u2002 "
            f"`\u200e {opponent_destruction:<8}\u200f`",
        )
    )


def _war_roster(clan: dict[str, Any], emojis: WarEmojiSet) -> str:
    counts = Counter(
        _as_int(member.get("townhallLevel"))
        for member in clan.get("members") or []
        if _as_int(member.get("townhallLevel")) > 0
    )
    entries = [
        f"{emojis.town_hall(level)} {emojis.number(total)}"
        for level, total in sorted(counts.items(), reverse=True)
    ]
    if not entries:
        return "-"
    return "\n".join(
        " ".join(entries[index:index + 5])
        for index in range(0, len(entries), 5)
    )


def build_war_board_embed(
    data: dict[str, Any],
    emojis: WarEmojiSet,
    *,
    timestamp: datetime | None = None,
) -> discord.Embed:
    clan = data.get("clan") or {}
    opponent = data.get("opponent") or {}
    state = normalize_war_state(data.get("state"))
    clan_name = str(clan.get("name") or "Clan")
    clan_tag = str(clan.get("tag") or "")
    opponent_name = discord.utils.escape_markdown(
        str(opponent.get("name") or "Unknown")
    )
    opponent_tag = str(opponent.get("tag") or "")
    embed = discord.Embed(
        title=f"{clan_name} ({clan_tag})",
        url=_clan_url(clan_tag),
        color=discord.Color(
            WAR_RESULT_COLORS[_war_result(clan, opponent)]
            if state == "warended"
            else WAR_STATE_COLORS.get(state, WAR_STATE_COLORS["preparation"])
        ),
        timestamp=timestamp,
    )
    embed.set_thumbnail(url=_badge_url(clan))

    description = [
        "**War Against**",
        f"**[{opponent_name} ({opponent_tag})]({_clan_url(opponent_tag)})**",
        "",
        "**War State**",
    ]
    if state == "preparation":
        start_time = coc_time_to_datetime(data.get("startTime"))
        description.append("Preparation Day")
        if start_time:
            description.append(
                f"War Start Time: {discord.utils.format_dt(start_time, 'R')}"
            )
    elif state == "inwar":
        end_time = coc_time_to_datetime(data.get("endTime"))
        description.append("Battle Day")
        if end_time:
            description.append(f"End Time: {discord.utils.format_dt(end_time, 'R')}")
    else:
        result = {
            "won": "Victory",
            "lost": "Defeat",
            "tied": "Draw",
        }[_war_result(clan, opponent)]
        description.append(f"War Ended — {result}")

    description.extend(
        (
            "",
            "**War Size**",
            f"{_as_int(data.get('teamSize'))} vs {_as_int(data.get('teamSize'))}",
        )
    )
    if state in {"inwar", "warended"}:
        description.extend(
            ("", "**War Stats**", _war_stats(data, clan, opponent, emojis))
        )
    description.extend(
        (
            "",
            "**Rosters**",
            discord.utils.escape_markdown(clan_name),
            _war_roster(clan, emojis),
            "",
            discord.utils.escape_markdown(str(opponent.get("name") or "Unknown")),
            _war_roster(opponent, emojis),
        )
    )
    embed.description = "\n".join(description)
    return embed


def build_war_summary_embed(
    data: dict[str, Any],
    emojis: WarEmojiSet,
    *,
    timestamp: datetime,
) -> discord.Embed:
    clan = data.get("clan") or {}
    opponent = data.get("opponent") or {}
    clan_name = discord.utils.escape_markdown(
        str(clan.get("name") or "Clan")
    )
    clan_tag = str(clan.get("tag") or "")
    opponent_name = discord.utils.escape_markdown(
        str(opponent.get("name") or "Unknown")
    )
    opponent_tag = str(opponent.get("tag") or "")
    embed = discord.Embed(
        title="Clan War Ended",
        description="\n".join(
            (
                "**War Against**",
                f"**[{opponent_name} ({opponent_tag})]({_clan_url(opponent_tag)})**",
                "",
                "**War Stats**",
                _war_stats(data, clan, opponent, emojis),
            )
        ),
        color=discord.Color(WAR_RESULT_COLORS[_war_result(clan, opponent)]),
        timestamp=timestamp,
    )
    embed.set_author(
        name=clan_name,
        url=_clan_url(clan_tag),
        icon_url=_badge_url(clan),
    )
    return embed


def coc_time_to_datetime(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y%m%dT%H%M%S.%fZ", "%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
