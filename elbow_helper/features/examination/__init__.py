"""Examination package entrypoint."""

from __future__ import annotations

from discord.ext import commands

from .cog import Examination
from .routing import ExamRoutingView
from .state import ExaminationStateStore


async def setup(bot: commands.Bot) -> None:
    cog = Examination(bot, ExaminationStateStore())
    await bot.add_cog(cog)
    bot.add_view(cog.panel_view)
    bot.add_view(ExamRoutingView(cog))


__all__ = ["Examination", "setup"]
