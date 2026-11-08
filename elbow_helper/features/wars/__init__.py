import asyncio
import discord

from elbow_helper.core.lifecycle import ElbowHelperBot
from elbow_helper.configuration.guild import GUILD_ID

from .cog import WarManager
from .commands import WarStatements
from .queries import WarQueries


async def setup(bot: ElbowHelperBot) -> None:
    await bot.add_cog(await asyncio.to_thread(WarManager, bot))
    await bot.add_cog(WarStatements(bot), guild=discord.Object(GUILD_ID))


__all__ = ["WarManager", "WarQueries", "setup"]
