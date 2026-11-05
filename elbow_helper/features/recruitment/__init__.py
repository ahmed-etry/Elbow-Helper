import discord
from elbow_helper.configuration.guild import GUILD_ID

from .cog import Recruitment
from .state import RecruitmentState
from .queries import RecruitmentQueries

__all__ = ["RecruitmentQueries", "setup"]


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
            recruitment_state=RecruitmentState(bot.workflow_events),
        ),
        guild=discord.Object(GUILD_ID),
    )
