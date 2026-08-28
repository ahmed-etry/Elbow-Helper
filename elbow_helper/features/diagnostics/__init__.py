from elbow_helper.core.lifecycle import ElbowHelperBot

from .cog import DebugCog


async def setup(bot: ElbowHelperBot) -> None:
    await bot.add_cog(DebugCog(bot, bot.clash_client))
