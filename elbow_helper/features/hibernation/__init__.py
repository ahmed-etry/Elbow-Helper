from __future__ import annotations

import discord
from discord.ext import commands

from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.discord.views import TranscriptLinkPromptView

from .cog import Hibernate
from .views import CloseTicketConfirmView, CloseTicketView, ReactivateView


async def setup(bot: commands.Bot) -> None:
    achievements = bot.get_cog("Achievements")
    if achievements is None:
        raise RuntimeError("Hibernation requires Achievements")
    cog = Hibernate(bot, achievements.rewards)
    guild = discord.Object(id=GUILD_ID)
    bot.tree.add_command(cog.hibernate_user, guild=guild)
    bot.tree.add_command(cog.reactivate, guild=guild)
    bot.add_view(CloseTicketView(cog))
    bot.add_view(CloseTicketConfirmView(cog))
    bot.add_view(ReactivateView(cog))
    bot.add_view(TranscriptLinkPromptView("hibernation_transcript_link"))
    await bot.add_cog(cog)
