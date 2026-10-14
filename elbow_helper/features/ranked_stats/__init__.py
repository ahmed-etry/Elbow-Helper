"""Ranked player observation feature."""

from .cog import RankedStats


async def setup(bot):
    links = bot.get_cog("AccountLinks")
    if links is None:
        raise RuntimeError("RankedStats requires AccountLinks")
    await bot.add_cog(RankedStats(bot, links))
