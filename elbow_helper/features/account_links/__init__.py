from __future__ import annotations

import discord

from elbow_helper.core.lifecycle import ElbowHelperBot
from elbow_helper.configuration.guild import GUILD_ID

from .cog import AccountLinks


async def setup(bot: ElbowHelperBot) -> None:
    cog = AccountLinks(bot, bot.clash_client)
    guild = discord.Object(id=GUILD_ID)
    await bot.add_cog(cog, guild=guild)
    war_manager = bot.get_cog("WarManager")
    if war_manager is None:
        raise RuntimeError("AccountLinks requires WarManager")
    war_manager.set_account_links(cog)
