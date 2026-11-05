
"""Clan-health package entrypoint."""

from __future__ import annotations

import discord

from elbow_helper.core.lifecycle import ElbowHelperBot
from elbow_helper.configuration.guild import GUILD_ID

from .analysis import ClanHealthAnalyzer
from .cog import ClanHealth
from .record import HealthRecord
from .record_wars import RegularWars


async def setup(bot: ElbowHelperBot) -> None:
    record = HealthRecord(bot.activity_reads, bot.raid_reads, bot.stay_reads)
    analyzer = ClanHealthAnalyzer(record, RegularWars(bot.war_reads), RegularWars(bot.war_reads, types=("CWL",)))
    cog = ClanHealth(
        bot,
        bot.google_publisher,
        bot.local_exports,
        record,
        analyzer,
    )
    guild = discord.Object(id=GUILD_ID)
    await bot.add_cog(cog, guild=guild)


__all__ = ["ClanHealth", "setup"]
