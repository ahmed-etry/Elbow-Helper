"""Clan data cog shell and lifecycle wiring."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
import logging
from typing import Any, Optional

import discord
from discord.ext import commands

from elbow_helper.infrastructure.clash import ClashClient
from elbow_helper.configuration.channels import CLAN_LEADERSHIP_CHANNELS
from elbow_helper.configuration.roles import ELDER_ROLE_ID

from .api import ClanReportingApiMixin
from .elders import ClanReportingElderMixin
from .helpers import ClanReportingHelperMixin
from .state import load_state
from .wars import ClanReportingWarMixin


LOGGER = logging.getLogger(__name__)


class ClanReporting(
    ClanReportingWarMixin,
    ClanReportingElderMixin,
    ClanReportingApiMixin,
    ClanReportingHelperMixin,
    commands.Cog,
):
    """Monthly war summaries and missing-elder leadership boards."""

    def __init__(
        self,
        bot: commands.Bot,
        clash_client: ClashClient,
        account_links,
    ):
        self.bot = bot
        self.clash_client = clash_client
        self.account_links = account_links
        self.state = load_state()
        self._board_last_repost_at: dict[str, float] = {}
        self._board_repost_locks: dict[str, asyncio.Lock] = {}
        self._refresh_task: Optional[asyncio.Task] = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._monthly_summary_loop.start()

    def cog_unload(self):
        self._monthly_summary_loop.cancel()
        for task in tuple(self._background_tasks):
            if not task.done():
                task.cancel()

    def _start_background_task(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str,
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)

        def finish(completed: asyncio.Task[None]) -> None:
            self._background_tasks.discard(completed)
            try:
                completed.result()
            except asyncio.CancelledError:
                return
            except Exception:
                LOGGER.exception("Clan Reporting background task failed: %s", name)

        task.add_done_callback(finish)
        return task

    @commands.Cog.listener()
    async def on_ready(self):
        if self._refresh_task and not self._refresh_task.done():
            return
        self._refresh_task = self._start_background_task(
            self._refresh_all_elder_lists(),
            name="clan-reporting-ready-refresh",
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author is None:
            return

        clan_code = self._leadership_channel_to_clan(message.channel.id)
        if not clan_code:
            return

        board_message_id = self.state.get("missing_elder_messages", {}).get(clan_code)
        if board_message_id and int(board_message_id) == message.id:
            return

        await self._repost_missing_elder_message_from_activity(clan_code, message)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        before_roles = {role.id for role in before.roles}
        after_roles = {role.id for role in after.roles}
        if before_roles == after_roles:
            return

        elder_changed = (ELDER_ROLE_ID in before_roles) != (ELDER_ROLE_ID in after_roles)
        if not elder_changed:
            return

        for clan_code in CLAN_LEADERSHIP_CHANNELS:
            self._start_background_task(
                self._update_missing_elder_message(clan_code),
                name=f"missing-elder-refresh:{clan_code}",
            )

    async def refresh_missing_elder_board(self, interaction: discord.Interaction, clan_code: str) -> None:
        try:
            await interaction.response.defer()
        except discord.NotFound:
            return
        await self.account_links.refresh_now(refresh_boards=False)
        await self._update_missing_elder_message(clan_code, reposition_if_buried=False)

    async def refresh_missing_elder_board_now(self, clan_code: str, *, reposition_if_buried: bool = False) -> None:
        await self._update_missing_elder_message(clan_code, reposition_if_buried=reposition_if_buried)

    async def refresh_all_missing_elder_boards_from_links(self) -> None:
        for clan_code in CLAN_LEADERSHIP_CHANNELS:
            await self._update_missing_elder_message(clan_code)
            await asyncio.sleep(0.2)
