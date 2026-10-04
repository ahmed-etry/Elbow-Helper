"""Requester controls for an answer whose sources are restricted here."""

from __future__ import annotations

import asyncio
import logging

import discord

from ..access import AgentAccessLost, require_evidence_access
from ..wording import ACTION_POST_HERE_BUTTON, ACTION_RESULT_EXPIRED, ACTION_RESULT_OWNER
from .private_view import PRIVATE_RESULT_TIMEOUT, PrivateResultView

LOGGER = logging.getLogger(__name__)


class PrivateAnswerView(PrivateResultView):
    def __init__(self, context, response, attachments, post,
                 *, timeout=PRIVATE_RESULT_TIMEOUT, private_view=None):
        super().__init__(
            context.member.id, (response,) + (private_view.parts if private_view else ()),
            tuple(attachments) + (private_view.attachments if private_view else ()),
            panels=private_view.panels if private_view else (),
            panel_labels=private_view.panel_labels if private_view else (),
        )
        self.timeout = timeout
        self.context = context
        self.post = post
        self._lock = asyncio.Lock()
        button = discord.ui.Button(label=ACTION_POST_HERE_BUTTON,
                                   style=discord.ButtonStyle.secondary)
        button.callback = self.post_here
        self.add_item(button)

    async def _authorize(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(ACTION_RESULT_OWNER, ephemeral=True)
            return False
        if not self.expired:
            try:
                await require_evidence_access(self.context)
                return True
            except AgentAccessLost:
                await self.on_timeout()
        await interaction.response.send_message(ACTION_RESULT_EXPIRED, ephemeral=True)
        return False

    async def open_result(self, interaction):
        async with self._lock:
            if await self._authorize(interaction):
                await super().open_result(interaction)

    async def post_here(self, interaction):
        async with self._lock:
            if not await self._authorize(interaction):
                return
            await interaction.response.defer()
            await self.post(interaction)
            LOGGER.info(
                "Agent answer disclosure overridden: requester=%s channel=%s sources=%s levels=%s",
                self.owner_id, self.context.source_message.channel.id,
                sorted(self.context.state.source_channels), sorted(self.context.state.required_access),
            )
            self.expired = True
            self.stop()
