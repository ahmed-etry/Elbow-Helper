"""Clan data package."""

from __future__ import annotations

import asyncio

import logging

import discord

from elbow_helper.core.lifecycle import ElbowHelperBot
from .state import load_state
from .cog import ClanReporting
from elbow_helper.configuration.channels import CLAN_LEADERSHIP_CHANNELS
from .views import MissingElderBoardView


LOGGER = logging.getLogger(__name__)


async def setup(bot: ElbowHelperBot) -> None:
    account_links = bot.get_cog("AccountLinks")
    if account_links is None:
        raise RuntimeError("ClanReporting requires AccountLinks")
    cog = ClanReporting(bot, account_links, initial_state=await asyncio.to_thread(load_state))
    await bot.add_cog(cog)
    account_links.set_board_refresher(cog)
    for clan_code in CLAN_LEADERSHIP_CHANNELS:
        try:
            bot.add_view(MissingElderBoardView(cog, clan_code))
        except (discord.HTTPException, RuntimeError, TypeError, ValueError):
            LOGGER.exception("Failed to register MissingElderBoardView for %s", clan_code)
