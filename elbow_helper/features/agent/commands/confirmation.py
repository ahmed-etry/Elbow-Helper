"""Show proposed changes and queue one confirmed run."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from ..actions.contracts import ActionClass, ChangePreview, PreparedAction, check_bundle
from ..wording import (
    ACTION_CANNOT_UNDO, ACTION_PREVIEW_SUMMARY,
    ACTION_PREVIEW_UNIT_MANY, ACTION_PREVIEW_UNIT_ONE,
    COMMAND_CANCEL_BUTTON, COMMAND_CANCELLED, COMMAND_CONFIRM_BUTTON,
    COMMAND_PREVIEW_EXPIRED,
    COMMAND_PREVIEW_HEADER, COMMAND_PREVIEW_OWNER,
    COMMAND_PREVIEW_USED, COMMAND_UNAVAILABLE,
)
from .private_view import PrivateCommandView


LOGGER = logging.getLogger(__name__)
CONFIRMATION_TIMEOUT = 300.0
PreparedCommand = PreparedAction


def preview_text(proposals: list[PreparedCommand]) -> str:
    check_bundle(tuple(proposals))
    lines = []
    for index, proposal in enumerate(proposals, start=1):
        count = proposal.preview.count
        lines.append(ACTION_PREVIEW_SUMMARY.format(
            index=index, name=proposal.preview.summary or proposal.path,
            count=count,
            unit=ACTION_PREVIEW_UNIT_ONE if count == 1 else ACTION_PREVIEW_UNIT_MANY,
        ))
        lines.extend(line.strip() or "-" for line in proposal.preview.lines)
        if proposal.action_class is ActionClass.IRREVERSIBLE:
            lines.append(ACTION_CANNOT_UNDO)
    return COMMAND_PREVIEW_HEADER + "\n" + "\n".join(lines)


class ConfirmationView(discord.ui.View):
    def __init__(self, owner_id: int, proposals: tuple[PreparedCommand, ...], context: Any,
                 private_result: PrivateCommandView | None = None, runner: Any = None):
        super().__init__(timeout=CONFIRMATION_TIMEOUT)
        check_bundle(proposals)
        self.owner_id = owner_id
        self.proposals = proposals
        self.context = context
        self.runner = runner
        self.message = None
        self.preview = preview_text(proposals)
        self.expired = False
        self.used = False
        self._lock = asyncio.Lock()
        confirm = discord.ui.Button(label=COMMAND_CONFIRM_BUTTON, style=discord.ButtonStyle.success)
        cancel = discord.ui.Button(label=COMMAND_CANCEL_BUTTON, style=discord.ButtonStyle.secondary)
        confirm.callback = self.confirm
        cancel.callback = self.cancel
        self.add_item(confirm)
        self.add_item(cancel)
        if private_result is not None:
            private_button = discord.ui.Button(
                label=private_result.children[0].label,
                style=discord.ButtonStyle.secondary,
            )
            private_button.callback = private_result.open_result
            self.add_item(private_button)

    def _disable(self) -> None:
        for item in self.children[:2]:
            item.disabled = True

    def _replacement(self, notice: str) -> str:
        content = getattr(self.message, "content", "")
        tail = content[len(self.preview):] if content.startswith(self.preview) else ""
        return notice + tail

    async def _claim(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(COMMAND_PREVIEW_OWNER, ephemeral=True)
            return False
        if self.expired:
            await interaction.response.send_message(COMMAND_PREVIEW_EXPIRED, ephemeral=True)
            return False
        if self.used:
            await interaction.response.send_message(COMMAND_PREVIEW_USED, ephemeral=True)
            return False
        self.used = True
        self._disable()
        return True

    async def cancel(self, interaction: discord.Interaction) -> None:
        async with self._lock:
            if not await self._claim(interaction):
                return
            await interaction.response.edit_message(
                content=self._replacement(COMMAND_CANCELLED), view=self,
            )
            LOGGER.info("Agent command preview cancelled: requester=%s", self.owner_id)

    async def confirm(self, interaction: discord.Interaction) -> None:
        async with self._lock:
            if not await self._claim(interaction):
                return
            await interaction.response.defer()
            try:
                if self.runner is None:
                    raise RuntimeError("Action runner is unavailable")
                await self.runner.submit(
                    self.context, self.proposals, confirmer_id=interaction.user.id,
                )
                LOGGER.info("Agent preview confirmed: requester=%s", self.owner_id)
            except Exception:
                LOGGER.exception("Agent preview could not be queued: requester=%s", self.owner_id)
                await self.context.source_message.channel.send(
                    COMMAND_UNAVAILABLE, allowed_mentions=discord.AllowedMentions.none(),
                )
            finally:
                if self.message is not None:
                    try:
                        await self.message.edit(view=self)
                    except discord.DiscordException:
                        LOGGER.warning("Agent preview could not be disabled")

    async def invalidate(self) -> None:
        async with self._lock:
            if self.used or self.expired:
                return
            self.used = True
            self._disable()
            if self.message is not None:
                try:
                    await self.message.edit(view=self)
                except discord.DiscordException:
                    LOGGER.warning("Agent preview could not be invalidated")

    async def on_timeout(self) -> None:
        self.expired = True
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    **({} if self.used else {"content": self._replacement(COMMAND_PREVIEW_EXPIRED)}),
                    view=self,
                )
            except discord.DiscordException:
                LOGGER.warning("Agent command preview could not expire")
