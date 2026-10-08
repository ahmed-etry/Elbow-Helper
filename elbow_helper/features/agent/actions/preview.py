"""Show proposed changes and queue one confirmed run."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from .contracts import ActionClass, PreparedAction, check_bundle
from .details import detail_lines
from ..access import AgentAccessLost, accessible_message_channel, require_access, require_access_requirements
from ..text import chunk_response
from ..wording import (
    ACTION_CANNOT_UNDO, ACTION_PREVIEW_SUMMARY, ACTION_PREVIEW_BLANK,
    ACTION_PREVIEW_UNIT_MANY, ACTION_PREVIEW_UNIT_ONE,
    ACTION_CANCEL_BUTTON, ACTION_CANCELLED, ACTION_CONFIRM_BUTTON,
    ACTION_PREVIEW_EXPIRED,
    ACTION_PREVIEW_HEADER, ACTION_PREVIEW_OWNER,
    ACTION_PREVIEW_USED, ACTION_UNAVAILABLE,
    ACTION_PREVIEW_DETAILS_HIDDEN, ACTION_PREVIEW_DETAILS_BUTTON,
    ACTION_RESULT_OWNER, ACTION_RESULT_EXPIRED,
)
from .private_view import PrivateResultView


LOGGER = logging.getLogger(__name__)
CONFIRMATION_TIMEOUT = 300.0
__all__ = ["ConfirmationView", "preview_text"]


def preview_text(proposals: list[PreparedAction]) -> str:
    check_bundle(tuple(proposals))
    lines = []
    groups: list[list[PreparedAction]] = []
    for proposal in proposals:
        if (groups and (groups[-1][0].preview.summary or groups[-1][0].path,
                        groups[-1][0].action_class) ==
                (proposal.preview.summary or proposal.path, proposal.action_class)):
            groups[-1].append(proposal)
        else:
            groups.append([proposal])
    for index, group in enumerate(groups, start=1):
        proposal = group[0]
        count = sum(item.preview.count for item in group)
        lines.append(ACTION_PREVIEW_SUMMARY.format(
            index=index, name=proposal.preview.summary or proposal.path,
            count=count,
            unit=ACTION_PREVIEW_UNIT_ONE if count == 1 else ACTION_PREVIEW_UNIT_MANY,
        ))
        for item in group:
            preview = item.preview
            hidden_fallback = item.details_hidden and not preview.details
            if not hidden_fallback:
                lines.extend(part.strip() or ACTION_PREVIEW_BLANK for line in preview.lines
                             for part in line.split("\n"))
            if item.details_hidden:
                lines.append(ACTION_PREVIEW_DETAILS_HIDDEN)
            else:
                lines.extend(part.strip() or ACTION_PREVIEW_BLANK for line in preview.details
                             for part in line.split("\n"))
        if proposal.action_class is ActionClass.IRREVERSIBLE:
            lines.append(ACTION_CANNOT_UNDO)
    return ACTION_PREVIEW_HEADER + "\n" + "\n".join(lines)


class ConfirmationView(discord.ui.View):
    def __init__(self, owner_id: int, proposals: tuple[PreparedAction, ...], context: Any,
                 private_result: PrivateResultView | None = None, runner: Any = None,
                 *, timeout: float = CONFIRMATION_TIMEOUT):
        super().__init__(timeout=timeout)
        check_bundle(proposals)
        self.owner_id = owner_id
        self.proposals = proposals
        self.context = context
        self.runner = runner
        self.private_result = private_result
        self.message = None
        self.preview = preview_text(proposals)
        self.expired = False
        self.details_expired = False
        self.used = False
        self._lock = asyncio.Lock()
        confirm = discord.ui.Button(label=ACTION_CONFIRM_BUTTON, style=discord.ButtonStyle.success)
        cancel = discord.ui.Button(label=ACTION_CANCEL_BUTTON, style=discord.ButtonStyle.secondary)
        confirm.callback = self.confirm
        cancel.callback = self.cancel
        self.add_item(confirm)
        self.add_item(cancel)
        if any(proposal.details_hidden for proposal in proposals):
            details_button = discord.ui.Button(
                label=ACTION_PREVIEW_DETAILS_BUTTON, style=discord.ButtonStyle.secondary,
            )
            details_button.callback = self.show_details
            self.add_item(details_button)
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

    async def show_details(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(ACTION_RESULT_OWNER, ephemeral=True)
            return
        if self.expired or self.details_expired:
            await interaction.response.send_message(ACTION_RESULT_EXPIRED, ephemeral=True)
            return
        try:
            require_access(self.context.guild, self.owner_id, self.context.source_message.channel)
            for action in self.proposals:
                if not action.details_hidden:
                    continue
                require_access_requirements(
                    self.context.guild, self.owner_id, action.preview.detail_access,
                )
                for identifier in action.preview.detail_sources:
                    if await accessible_message_channel(self.context, identifier) is None:
                        raise AgentAccessLost("Preview source is no longer readable")
        except AgentAccessLost:
            self.details_expired = True
            await interaction.response.send_message(ACTION_RESULT_EXPIRED, ephemeral=True)
            return
        parts = chunk_response("\n".join(
            line for action in self.proposals if action.details_hidden
            for line in detail_lines(action)
        ))
        for index, part in enumerate(parts):
            sender = interaction.followup.send if index else interaction.response.send_message
            await sender(part, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    def _replacement(self, notice: str) -> str:
        content = getattr(self.message, "content", "")
        tail = content[len(self.preview):] if content.startswith(self.preview) else ""
        return notice + tail

    async def _claim(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(ACTION_PREVIEW_OWNER, ephemeral=True)
            return False
        if self.expired:
            await interaction.response.send_message(ACTION_PREVIEW_EXPIRED, ephemeral=True)
            return False
        if self.used:
            await interaction.response.send_message(ACTION_PREVIEW_USED, ephemeral=True)
            return False
        self.used = True
        self._disable()
        return True

    async def cancel(self, interaction: discord.Interaction) -> None:
        async with self._lock:
            if not await self._claim(interaction):
                return
            if getattr(self.message, "preserves_other_text", False):
                await interaction.response.defer()
                await self.message.edit(content=ACTION_CANCELLED, view=self)
            else:
                await interaction.response.edit_message(
                    content=self._replacement(ACTION_CANCELLED), view=self,
                )
            LOGGER.info("Agent command preview cancelled: requester=%s", self.owner_id)

    async def confirm(self, interaction: discord.Interaction) -> None:
        async with self._lock:
            if not await self._claim(interaction):
                return
            await interaction.response.defer()
            if self.message is not None:
                try:
                    await self.message.edit(view=self)
                except discord.DiscordException:
                    LOGGER.warning("Agent preview could not be disabled")
            progress_message = self._progress_message()
            try:
                if self.runner is None:
                    raise RuntimeError("Action runner is unavailable")
                await self.runner.submit(
                    self.context, self.proposals, confirmer_id=interaction.user.id,
                    progress_message=progress_message,
                )
                LOGGER.info("Agent preview confirmed: requester=%s", self.owner_id)
            except Exception:
                LOGGER.exception("Agent preview could not be queued: requester=%s", self.owner_id)
                await (
                    getattr(self.context, "delivery_channel", None)
                    or self.context.source_message.channel
                ).send(
                    ACTION_UNAVAILABLE, allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                if progress_message is not None:
                    # The run owns the message now; a later timeout must not edit it.
                    self.stop()

    def _progress_message(self):
        """Hand the run an edit target that owns only the preview's content."""
        if getattr(self.message, "preserves_other_text", False):
            return self.message
        if (self.private_result is None
                and getattr(self.message, "content", None) == preview_text(self.proposals)):
            return self.message
        return None

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
                    **({} if self.used else {"content": self._replacement(ACTION_PREVIEW_EXPIRED)}),
                    view=self,
                )
            except discord.DiscordException:
                LOGGER.warning("Agent command preview could not expire")
