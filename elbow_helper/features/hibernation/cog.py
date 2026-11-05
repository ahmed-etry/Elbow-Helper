from __future__ import annotations

from discord.ext import commands

from elbow_helper.configuration.guild import GUILD_ID

from .commands import HibernationCommandMixin
from .state import HibernationState, HibernationStateReader
from .queries import HibernationQueries
from .tickets import HibernationTicketMixin


class Hibernate(commands.Cog, HibernationTicketMixin, HibernationCommandMixin):
    def __init__(self, bot: commands.Bot, achievement_rewards):
        self.bot = bot
        self.achievement_rewards = achievement_rewards
        self.hibernation_state = HibernationState(bot.workflow_events)
        self.reader = HibernationStateReader(self.hibernation_state)
        self.queries = HibernationQueries(self.hibernation_state.snapshot)
        self._fallback_info_ready = False

    async def cog_load(self):
        await self.hibernation_state.startup()

    def cog_unload(self):
        self.hibernation_state.events.unregister_member_deletion("hibernation")

    @commands.Cog.listener()
    async def on_ready(self):
        if self._fallback_info_ready:
            return
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return
        await self.ensure_fallback_info_message(guild)
        self._fallback_info_ready = True

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        async with self.hibernation_state.member_lock(member.id):
            await self._delete_fallback_thread_for_member(member.id)
            await self.hibernation_state.save_member(member.id, None)
