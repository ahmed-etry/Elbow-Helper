
"""Clan-health package entrypoint."""

from __future__ import annotations

import discord

from elbow_helper.core.lifecycle import ElbowHelperBot
from elbow_helper.configuration.guild import GUILD_ID

from .analysis import ClanHealthAnalyzer
from .api import ClanHealthCollector
from .database import ClanHealthRepository
from .cog import ClanHealth


async def setup(bot: ElbowHelperBot) -> None:
    repository = ClanHealthRepository()
    analyzer = ClanHealthAnalyzer(repository)
    collector = ClanHealthCollector(
        bot.clash_client,
        repository,
        analyzer,
    )
    cog = ClanHealth(
        bot,
        bot.clash_client,
        bot.google_publisher,
        bot.local_exports,
        repository,
        analyzer,
        collector,
    )
    guild = discord.Object(id=GUILD_ID)
    await bot.add_cog(cog, guild=guild)


__all__ = ["ClanHealth", "setup"]
