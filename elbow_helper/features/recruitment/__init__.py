import discord
from elbow_helper.configuration.guild import GUILD_ID

from .cog import Recruitment
from .state import RecruitmentStateStore


async def setup(bot) -> None:
    account_links = bot.get_cog("AccountLinks")
    achievements = bot.get_cog("Achievements")
    if account_links is None or achievements is None:
        raise RuntimeError(
            "Recruitment requires AccountLinks and Achievements"
        )
    await bot.add_cog(
        Recruitment(
            bot,
            account_links=account_links,
            achievement_rewards=achievements.rewards,
            state_store=RecruitmentStateStore(),
        ),
        guild=discord.Object(GUILD_ID),
    )
