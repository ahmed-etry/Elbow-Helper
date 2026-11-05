from __future__ import annotations

import asyncio

from discord.ext import commands

from .ai import SupportWelcomeService
from .commands import SupportCommandMixin
from .routing import RoutingMixin
from .views import SupportTicketCloseView, SupportTicketConfirmView
from .queries import SupportTicketQueries
from .state import TicketState


class SupportActions(commands.Cog, SupportCommandMixin, RoutingMixin):
    """Support ticket open/close flow and ticket rename automation."""

    def __init__(
        self,
        bot: commands.Bot,
        welcome_messages: SupportWelcomeService,
    ):
        self.bot = bot
        self.welcome_messages = welcome_messages
        self.ticket_state = TicketState(bot.workflow_events)
        self.queries = SupportTicketQueries()
        self._scan_task = None

    async def cog_load(self):
        await self.ticket_state.startup()
        self._scan_task = asyncio.create_task(self.scan_existing_tickets())

    def cog_unload(self):
        self.ticket_state.events.unregister_member_deletion("support")
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        if self.ticket_state.registration(channel.id) is not None:
            await self.ticket_state.channel_deleted(channel.id)

    def build_confirm_view(self) -> SupportTicketConfirmView:
        return SupportTicketConfirmView(self)

    def build_close_view(self) -> SupportTicketCloseView:
        return SupportTicketCloseView(self)
