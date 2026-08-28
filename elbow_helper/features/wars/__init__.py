import discord

from elbow_helper.core.lifecycle import ElbowHelperBot
from elbow_helper.configuration.guild import GUILD_ID

from .cog import WarManager
from .commands import WarStatements


async def setup(bot: ElbowHelperBot) -> None:
    await bot.add_cog(WarManager(bot, bot.clash_client))
    await bot.add_cog(WarStatements(bot), guild=discord.Object(GUILD_ID))
