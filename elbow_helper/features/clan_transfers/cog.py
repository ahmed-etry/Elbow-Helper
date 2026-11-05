"""Main clan transfers cog lifecycle and slash-command wiring."""

from __future__ import annotations

import asyncio
import logging

from discord.ext import commands

from elbow_helper.core.background import start_resilient_loop
from .commands import ClanTransfersCommandMixin
from .config import CLAN_TRANSFER_QUEUES
from .queues import ClanTransferQueueMixin
from .queries import ClanTransferQueries
from .state import TransferState
from .views import ClanTransfersView


class ClanTransfers(ClanTransferQueueMixin, ClanTransfersCommandMixin, commands.Cog):
    """Clan transfer queue board workflows."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.logger = logging.getLogger(__name__)
        self.state_store = TransferState(bot.workflow_events, clock=self._now)
        self.queries = ClanTransferQueries(lambda: self.state, clock=self._now)
        self.locks = {code: asyncio.Lock() for code in CLAN_TRANSFER_QUEUES}
        self.global_lock = asyncio.Lock()
        self._bootstrap_task = None

    @property
    def state(self):
        return self.state_store.state

    async def cog_load(self):
        await self.state_store.startup()
        start_resilient_loop(self.request_expiry_loop)
        self._bootstrap_task = asyncio.create_task(self._bootstrap())

    def cog_unload(self) -> None:
        self.state_store.events.unregister_member_deletion("transfers")
        self.request_expiry_loop.cancel()
        if self._bootstrap_task and not self._bootstrap_task.done():
            self._bootstrap_task.cancel()


async def setup(bot: commands.Bot) -> None:
    cog = ClanTransfers(bot)
    await bot.add_cog(cog)

    for clan_code in CLAN_TRANSFER_QUEUES:
        bot.add_view(ClanTransfersView(clan_code))
