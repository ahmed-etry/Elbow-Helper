"""Main clan transfers cog lifecycle and slash-command wiring."""

from __future__ import annotations

import asyncio
import logging

from discord.ext import commands

from .commands import ClanTransfersCommandMixin
from .config import CLAN_TRANSFER_QUEUES
from .queues import ClanTransferQueueMixin
from .state import load_state
from .views import ClanTransfersView


class ClanTransfers(ClanTransferQueueMixin, ClanTransfersCommandMixin, commands.Cog):
    """Clan transfer queue board workflows."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.logger = logging.getLogger(__name__)
        self.state = load_state()
        self.locks = {code: asyncio.Lock() for code in CLAN_TRANSFER_QUEUES}
        self.global_lock = asyncio.Lock()
        self.request_expiry_loop.start()
        self._bootstrap_task = asyncio.create_task(self._bootstrap())

    def cog_unload(self) -> None:
        self.request_expiry_loop.cancel()
        if self._bootstrap_task and not self._bootstrap_task.done():
            self._bootstrap_task.cancel()


async def setup(bot: commands.Bot) -> None:
    cog = ClanTransfers(bot)
    await bot.add_cog(cog)

    for clan_code in CLAN_TRANSFER_QUEUES:
        bot.add_view(ClanTransfersView(clan_code))
