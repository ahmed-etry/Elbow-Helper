from __future__ import annotations

from discord.ext import commands

from elbow_helper.configuration.guild import GUILD_ID

from .commands import HibernationCommandMixin
from .state import HibernationStateReader
from .state import load_hibernation_state, save_hibernation_state
from .tickets import HibernationTicketMixin


class Hibernate(commands.Cog, HibernationTicketMixin, HibernationCommandMixin):
    def __init__(self, bot: commands.Bot, achievement_rewards):
        self.bot = bot
        self.achievement_rewards = achievement_rewards
        self.reader = HibernationStateReader()
        self._fallback_info_ready = False

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
        await self._delete_fallback_thread_for_member(
            member.id,
        )
        data = load_hibernation_state()
        if str(member.id) in data:
            del data[str(member.id)]
            save_hibernation_state(data)
