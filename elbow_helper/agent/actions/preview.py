"""Show proposed changes and queue one confirmed run."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from math import ceil
from time import monotonic
from dataclasses import replace
from typing import Any

import discord

from .contracts import ActionClass, PreparedAction, check_bundle
from .details import detail_lines
from elbow_helper.agent.safety.access import AgentAccessLost, accessible_message_channel, require_access, require_access_requirements
from elbow_helper.agent.conversation.state import ConversationTurn
from elbow_helper.agent.conversation.text import DISCORD_MESSAGE_LIMIT
from elbow_helper.agent.conversation.wording import (
    ACTION_CANNOT_UNDO, ACTION_PREVIEW_BLANK,
    ACTION_PREVIEW_UNIT_MANY, ACTION_PREVIEW_UNIT_ONE, ACTION_CANCEL_BUTTON,
    ACTION_CANCELLED, ACTION_CONFIRM_BUTTON, ACTION_CONFIRM_FOOTER,
    ACTION_CONFIRM_TITLE, ACTION_PREVIEW_EXPIRED, ACTION_PREVIEW_REPLACED,
    ACTION_PREVIEW_HEADER, ACTION_PREVIEW_OWNER, ACTION_PREVIEW_USED, ACTION_UNAVAILABLE,
    ACTION_PREVIEW_DETAILS_HIDDEN, ACTION_PREVIEW_DETAILS_BUTTON, ACTION_RESULT_OWNER,
    ACTION_RESULT_EXPIRED, LONG_REPLY_FILENAME,
)
from .private_view import PrivateResultView
from .message_cleanup import log_cleanup_failure


LOGGER = logging.getLogger(__name__)
CONFIRMATION_TIMEOUT = 300.0
__all__ = ["ConfirmationView", "is_preview_reply", "preview_text"]


def is_preview_reply(context, answer: str) -> bool:
    """Recognize trusted action text before checking model-authored mentions."""
    proposals = context.state.proposed_changes
    return bool(proposals and answer == preview_text(proposals))


def preview_text(proposals: list[PreparedAction]) -> str:
    check_bundle(tuple(proposals))
    blocks = ["\n".join(filter(None, _change_block(action))) for action in proposals]
    return ACTION_PREVIEW_HEADER + "\n" + "\n\n".join(blocks)


def _preview_lines(lines):
    return [part for line in lines for part in
            ("" if line == ACTION_PREVIEW_BLANK else line).split("\n")]


def _change_block(action, *, hide_for_length=False):
    preview = action.preview
    hidden_lines = action.details_hidden and (preview.preview_only_sources or not preview.details)
    lines = [] if hidden_lines else _preview_lines(
        preview.lines[:1] if hide_for_length else preview.lines,
    )
    if hide_for_length and not action.details_hidden and not preview.lines:
        lines = _preview_details(preview)[:1]
    if hide_for_length or action.details_hidden:
        lines.append(ACTION_PREVIEW_DETAILS_HIDDEN)
    else:
        lines.extend(_preview_details(preview))
    if action.action_class is ActionClass.IRREVERSIBLE:
        lines.append(ACTION_CANNOT_UNDO)
    return (lines[0], "\n".join(lines[1:])) if lines else ("", "")


def _preview_details(preview):
    if preview.message_body is None:
        return _preview_lines(preview.details)
    start, end, _ = preview.message_body.indices(len(preview.details))
    return [*_preview_lines(preview.details[:start]),
            *(f"> {part}" for line in preview.details[start:end] for part in line.split("\n")),
            *_preview_lines(preview.details[end:])]


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
        self.deadline = monotonic() + timeout
        self._countdown = None
        self.preview = preview_text(proposals)
        self.card, self.length_hidden = self._build_card()
        self.expired = False
        self.superseded = False
        self.details_expired = False
        self.used = False
        self._lock = asyncio.Lock()
        confirm = discord.ui.Button(label=ACTION_CONFIRM_BUTTON, style=discord.ButtonStyle.success)
        cancel = discord.ui.Button(label=ACTION_CANCEL_BUTTON, style=discord.ButtonStyle.secondary)
        confirm.callback = self.confirm
        cancel.callback = self.cancel
        self.add_item(confirm)
        self.add_item(cancel)
        if self.length_hidden or any(proposal.details_hidden for proposal in proposals):
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

    def _minutes_left(self):
        return max(0, ceil((self.deadline - monotonic()) / 60))

    def _set_footer(self, card):
        member = getattr(self.context, "member", None)
        card.set_footer(text=ACTION_CONFIRM_FOOTER.format(
            name=getattr(member, "display_name", None) or str(self.owner_id),
            minutes=self._minutes_left(),
        ))

    def _build_card(self):
        count = sum(action.preview.count for action in self.proposals)
        card = discord.Embed(colour=discord.Colour.blue(), title=ACTION_CONFIRM_TITLE.format(
            count=count, unit=ACTION_PREVIEW_UNIT_ONE if count == 1 else ACTION_PREVIEW_UNIT_MANY,
        ))
        self._set_footer(card)
        blocks = [_change_block(action) for action in self.proposals]
        description = "\n\n".join("\n".join(filter(None, block)) for block in blocks)
        if len(description) <= 4096:
            card.description = description
            return card, False
        if (len(blocks) <= 25 and all(0 < len(name) <= 256 and len(value) <= 1024 for name, value in blocks)
                and len(card) + sum(len(name) + (len(value) or 1) for name, value in blocks) <= 6000):
            for name, value in blocks:
                card.add_field(name=name, value=value or "\u200b", inline=False)
            return card, False
        description = "\n\n".join("\n".join(filter(None, _change_block(
            action, hide_for_length=True,
        ))) for action in self.proposals)
        if len(description) > 4096:
            description = ACTION_PREVIEW_DETAILS_HIDDEN
            if any(action.action_class is ActionClass.IRREVERSIBLE for action in self.proposals):
                description += "\n" + ACTION_CANNOT_UNDO
        card.description = description
        return card, True

    def start(self, message):
        self.message = message
        if not self.is_finished():
            self._countdown = asyncio.create_task(self._refresh_footer())

    async def _refresh_footer(self):
        while (minutes := self._minutes_left()) > 0:
            await asyncio.sleep(max(0, self.deadline - monotonic() - (minutes - 1) * 60))
            async with self._lock:
                if self.used or self.expired or self.is_finished():
                    return
                if self._minutes_left() and not self.superseded:
                    self._set_footer(self.card)
                    try:
                        await self.message.edit(embed=self.card)
                    except discord.DiscordException as error:
                        log_cleanup_failure(LOGGER, "Agent preview footer could not be updated", error)
        self._countdown = None
        await self.on_timeout()

    def stop(self):
        if self._countdown is not None:
            self._countdown.cancel()
            self._countdown = None
        super().stop()

    def _disable(self) -> None:
        for item in self.children[:2]:
            item.disabled = True

    def closed_turn(self, how: str) -> ConversationTurn:
        """The conversation's record that this card ended without running (cancelled, expired or replaced)."""
        return ConversationTurn(
            text=json.dumps({"confirmation": how, "actions": list(dict.fromkeys(
                proposal.preview.summary or proposal.path for proposal in self.proposals))}, ensure_ascii=False),
            source_channels=frozenset(self.context.state.source_channels),
            required_access=frozenset(self.context.state.required_access))

    async def _closed(self, how: str) -> None:
        retain = getattr(self.runner, "on_unconfirmed", None)
        if retain is None:
            return
        try:
            await retain(self, how)
        except Exception:
            LOGGER.exception("Agent preview outcome could not be retained: requester=%s", self.owner_id)

    async def show_details(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(ACTION_RESULT_OWNER, ephemeral=True)
            return
        if self.expired or self.superseded or self.details_expired or not self._minutes_left():
            await interaction.response.send_message(ACTION_RESULT_EXPIRED, ephemeral=True)
            return
        try:
            require_access(self.context.guild, self.owner_id, self.context.source_message.channel)
            for action in self.proposals:
                if not (action.details_hidden or self.length_hidden):
                    continue
                require_access_requirements(
                    self.context.guild, self.owner_id, action.preview.detail_access,
                )
                for identifier in action.preview.disclosure_sources:
                    if await accessible_message_channel(replace(
                        self.context, private_pass=True,
                        state=replace(self.context.state, scope=None, source_access={}),
                    ), identifier) is None:
                        raise AgentAccessLost("Preview source is no longer readable")
        except AgentAccessLost:
            self.details_expired = True
            await interaction.response.send_message(ACTION_RESULT_EXPIRED, ephemeral=True)
            return
        blocks = []
        for action in self.proposals:
            if self.length_hidden and not action.details_hidden:
                blocks.append("\n".join(filter(None, _change_block(action))))
            elif action.details_hidden:
                preview = action.preview
                lines = _preview_lines(detail_lines(action))
                if preview.message_body is not None:
                    lines = _preview_details(preview)
                    if preview.preview_only_sources:
                        lines[:0] = _preview_lines(preview.lines)
                blocks.append("\n".join(lines))
        details = "\n\n".join(blocks)
        options = {"ephemeral": True, "allowed_mentions": discord.AllowedMentions.none()}
        if len(details) <= DISCORD_MESSAGE_LIMIT:
            await interaction.response.send_message(details, **options)
            return
        file = discord.File(io.BytesIO(details.encode("utf-8")), filename=LONG_REPLY_FILENAME)
        try:
            await interaction.response.send_message(file=file, **options)
        finally:
            file.close()

    async def _claim(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(ACTION_PREVIEW_OWNER, ephemeral=True)
            return False
        if self.expired or self.superseded or not self._minutes_left():
            await interaction.response.send_message(
                ACTION_PREVIEW_EXPIRED if self.expired or not self._minutes_left()
                else ACTION_PREVIEW_REPLACED, ephemeral=True,
            )
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
            await interaction.response.edit_message(
                embed=discord.Embed(description=ACTION_CANCELLED, colour=discord.Colour.greyple()),
                view=self.private_result,
            )
            self.stop()
            LOGGER.info("Agent command preview cancelled: requester=%s", self.owner_id)
            await self._closed("cancelled")

    async def confirm(self, interaction: discord.Interaction) -> None:
        async with self._lock:
            if not await self._claim(interaction):
                return
            await interaction.response.defer()
            self.stop()
            try:
                if self.runner is None:
                    raise RuntimeError("Action runner is unavailable")
                await self.runner.submit(
                    self.context, self.proposals, confirmer_id=interaction.user.id,
                    progress_message=self.message, confirmation=self.message is not None,
                    **({"private_result": self.private_result} if self.private_result is not None else {}),
                )
                LOGGER.info("Agent preview confirmed: requester=%s", self.owner_id)
            except Exception:
                LOGGER.exception("Agent preview could not be queued: requester=%s", self.owner_id)
                if self.message is not None:
                    try:
                        await self.message.edit(embed=discord.Embed(
                            description=ACTION_UNAVAILABLE, colour=discord.Colour.red(),
                        ), view=self.private_result)
                    except discord.DiscordException as error:
                        log_cleanup_failure(LOGGER, "Agent preview could not show queue failure", error)
                else:
                    await interaction.followup.send(ACTION_UNAVAILABLE, ephemeral=True)
                await self._closed("unqueued")

    def supersede(self) -> None:
        # Keep the callback available until timeout so an old click gets a response.
        if not self.used and not self.expired:
            self.superseded = True

    async def on_timeout(self) -> None:
        async with self._lock:
            if self.used or self.expired:
                return
            self.expired = True
            for item in self.children:
                item.disabled = True
            if self.message is not None:
                try:
                    await self.message.edit(embed=discord.Embed(
                        description=ACTION_PREVIEW_EXPIRED, colour=discord.Colour.greyple(),
                    ), view=self.private_result)
                except discord.DiscordException as error:
                    log_cleanup_failure(LOGGER, "Agent command preview could not expire", error)
            self.stop()
            if not self.superseded:
                await self._closed("expired")
