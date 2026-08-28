from __future__ import annotations

import asyncio

from discord.ext import commands

from .ai import SupportWelcomeService
from .commands import SupportCommandMixin
from .routing import RoutingMixin
from .views import SupportTicketCloseView, SupportTicketConfirmView


class SupportActions(commands.Cog, SupportCommandMixin, RoutingMixin):
    """Support ticket open/close flow and ticket rename automation."""

    def __init__(
        self,
        bot: commands.Bot,
        welcome_messages: SupportWelcomeService,
    ):
        self.bot = bot
        self.welcome_messages = welcome_messages
        self._scan_task = asyncio.create_task(self.scan_existing_tickets())

    def cog_unload(self):
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()

    def build_confirm_view(self) -> SupportTicketConfirmView:
        return SupportTicketConfirmView(self)

    def build_close_view(self) -> SupportTicketCloseView:
        return SupportTicketCloseView(self)
