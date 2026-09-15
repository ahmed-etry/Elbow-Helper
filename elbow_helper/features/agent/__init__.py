"""Core-only mention-driven agent feature."""

from __future__ import annotations

from elbow_helper.discord.message_search import DiscordMessageSearch

from .cog import CoreAgent


async def setup(bot) -> None:
    account_links = bot.get_cog("AccountLinks")
    clan_health = bot.get_cog("ClanHealth")
    if account_links is None or clan_health is None:
        raise RuntimeError("Core agent requires AccountLinks and ClanHealth")
    await bot.add_cog(
        CoreAgent(
            bot,
            account_links=account_links,
            clan_health=clan_health,
            message_search=DiscordMessageSearch(bot.http),
        )
    )
