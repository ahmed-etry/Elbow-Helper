"""Hold proposed command changes until their requester confirms."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import io
import logging
from typing import Any

import discord

from ..access import AgentAccessLost, require_access, require_disclosure_access
from ..wording import (
    COMMAND_CANCEL_BUTTON, COMMAND_CANCELLED, COMMAND_CONFIRM_BUTTON,
    COMMAND_CONFIRM_FAILED, COMMAND_PREVIEW_CHANGED, COMMAND_PREVIEW_EXPIRED,
    COMMAND_PREVIEW_HEADER, COMMAND_PREVIEW_OWNER,
    COMMAND_PREVIEW_USED, COMMAND_NO_CHANGES,
)
from .outcomes import CommandOutcome, command_reply
from .private_view import PrivateCommandView


LOGGER = logging.getLogger(__name__)
CONFIRMATION_TIMEOUT = 300.0
MAX_PREVIEW_CHARACTERS = 1900


@dataclass(frozen=True, slots=True)
class ChangePreview:
    lines: tuple[str, ...]
    recheck: Callable[[], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class PreparedCommand:
    path: str
    values: Mapping[str, Any]
    preview: ChangePreview
    run: Callable[[], Awaitable[CommandOutcome]]


def preview_text(proposals: list[PreparedCommand]) -> str:
    lines = [line.strip() or "-" for proposal in proposals for line in proposal.preview.lines]
    return COMMAND_PREVIEW_HEADER + "\n" + "\n".join(lines or ["-"])


class ConfirmationView(discord.ui.View):
    def __init__(self, owner_id: int, proposals: tuple[PreparedCommand, ...], context: Any,
                 private_result: PrivateCommandView | None = None):
        super().__init__(timeout=CONFIRMATION_TIMEOUT)
        self.owner_id = owner_id
        self.proposals = proposals
        self.context = context
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
            started = False
            finished: list[str] = []
            try:
                require_access(self.context.guild, self.owner_id,
                               self.context.source_message.channel)
                await require_disclosure_access(self.context)
                checks = [await proposal.preview.recheck() for proposal in self.proposals]
                if not all(checks):
                    await interaction.followup.send(COMMAND_PREVIEW_CHANGED, ephemeral=True)
                    LOGGER.info("Agent command preview changed: requester=%s", self.owner_id)
                    return
                for proposal in self.proposals:
                    require_access(self.context.guild, self.owner_id,
                                   self.context.source_message.channel)
                    await require_disclosure_access(self.context)
                    started = True
                    outcome = await proposal.run()
                    if not isinstance(outcome, CommandOutcome):
                        raise TypeError("Command returned an invalid result")
                    finished.append(proposal.path)
                    await require_disclosure_access(self.context)
                    await self._send_outcome(interaction, outcome)
                    LOGGER.info("Agent command confirmed: requester=%s command=%s status=%s",
                                self.owner_id, proposal.path, outcome.status)
            except AgentAccessLost:
                await interaction.followup.send(
                    self._failure(finished) if started else COMMAND_PREVIEW_CHANGED,
                    ephemeral=True,
                )
                LOGGER.info("Agent command access changed: requester=%s", self.owner_id)
            except Exception:
                LOGGER.exception("Agent command confirmation failed: requester=%s", self.owner_id)
                await interaction.followup.send(self._failure(finished), ephemeral=True)
            finally:
                if self.message is not None:
                    try:
                        await self.message.edit(view=self)
                    except discord.DiscordException:
                        LOGGER.warning("Agent command preview could not be disabled")

    def _failure(self, finished: list[str]) -> str:
        remaining = [proposal.path for proposal in self.proposals[len(finished):]]
        return COMMAND_CONFIRM_FAILED.format(
            finished=", ".join(finished) or COMMAND_NO_CHANGES,
            remaining=", ".join(remaining) or COMMAND_NO_CHANGES,
        )

    async def _send_outcome(self, interaction: discord.Interaction, outcome: CommandOutcome) -> None:
        private = outcome.visibility == "private"
        if private:
            parts = ((outcome.text,) if outcome.text else ()) + outcome.private_parts
        else:
            parts = (outcome.text,)
        if not any(parts) and not outcome.attachments:
            parts = (command_reply([outcome]),)
        files = [discord.File(io.BytesIO(item.data), filename=item.filename)
                 for item in outcome.attachments]
        try:
            for index, part in enumerate(parts or ("",)):
                await interaction.followup.send(
                    part or None, files=files if index == 0 else [],
                    ephemeral=private,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        finally:
            for file in files:
                file.close()

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
