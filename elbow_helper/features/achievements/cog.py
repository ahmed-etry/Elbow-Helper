"""Achievements cog lifecycle and package wiring."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os

import discord
from discord.ext import commands
from elbow_helper.configuration.guild import GUILD_ID

from .achievements import AchievementServiceMixin
from .commands import AchievementCommandMixin
from .database import AchievementsDatabaseMixin
from .definitions import ALL_ACHIEVEMENTS
from .economy import AchievementEconomyMixin
from .progress import AchievementProgressMixin
from .raffle import AchievementRaffleMixin
from .rewards import AchievementRewardService
from .tracking import AchievementTrackingMixin
from .views import RaffleHubView


class Achievements(
    AchievementCommandMixin,
    AchievementRaffleMixin,
    AchievementEconomyMixin,
    AchievementProgressMixin,
    AchievementServiceMixin,
    AchievementTrackingMixin,
    AchievementsDatabaseMixin,
    commands.Cog,
):
    GUILD_ID = GUILD_ID
    ALL_ACHIEVEMENTS = ALL_ACHIEVEMENTS

    def __init__(self, bot):
        self.bot = bot
        self._db_lock = asyncio.Lock()
        self._raffle_hub_view_added = False
        self._raffle_hub_lock = asyncio.Lock()
        self._post_commit_actions_var = contextvars.ContextVar(
            "achievements_post_commit_actions",
            default=None,
        )
        self._last_cache_prune_ts = 0
        self._message_count_cooldowns = {}
        self._emoji_daily_counts = {}
        self._reaction_daily_counts = {}
        self._meme_daily_counts = {}
        self._meme_cooldowns = {}
        self._active_channel_daily_sets = {}
        self._clan_role_change_tasks = {}
        self._pending_clan_role_baselines = {}

        data_dir = bot.paths.data_root / "achievements"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = os.fspath(data_dir / "achievements.db")
        self.logger = logging.getLogger(__name__)
        self.init_database()
        self.rewards = AchievementRewardService(self)
        self.init_achievements()
        self.check_time_achievements.start()
        self.initial_achievement_check.start()
        self.cleanup_database.start()
        self.salary_task.start()
        self.raffle_hub_task.start()

    def cog_unload(self):
        for loop in (
            self.check_time_achievements,
            self.initial_achievement_check,
            self.cleanup_database,
            self.salary_task,
            self.raffle_hub_task,
        ):
            if loop.is_running():
                loop.cancel()
        for task in self._clan_role_change_tasks.values():
            if not task.done():
                task.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        await self.cleanup_old_achievements()
        if not self._raffle_hub_view_added:
            self.bot.add_view(RaffleHubView(self))
            self._raffle_hub_view_added = True
        await self.update_raffle_hub_message()


async def setup(bot):
    guild = discord.Object(id=GUILD_ID)
    await bot.add_cog(Achievements(bot), guild=guild)
