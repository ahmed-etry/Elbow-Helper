"""Requester-only controls for an explicitly requested extra evidence pass."""

from __future__ import annotations

import asyncio
import logging

import discord

from ..access import ACCESS_VIEW_AUDIT_LOG, AgentAccessLost, assert_answer_scope, require_evidence_access
from ..disclosure import log_scope_override
from ..wording import (
    ACTION_POST_HERE_BUTTON, ACTION_RESULT_EXPIRED, ACTION_RESULT_OWNER,
    ACTION_UNAVAILABLE, FAILURE_MESSAGE, AGENT_PRIVATE_CHECK_BUTTON,
)
from ..files.delivery import attachment_files
from ..discord_actions.message_parts import image_attachment, outgoing_parts
from ..engine.service import AgentUnavailableError
from .message_cleanup import log_cleanup_failure
from .private_view import PRIVATE_RESULT_TIMEOUT, PrivateResultView

LOGGER = logging.getLogger(__name__)


class PrivateAnswerView(PrivateResultView):
    def __init__(self, context, *, private_view=None):
        super().__init__(
            context.member.id, private_view.parts if private_view else (),
            private_view.attachments if private_view else (),
            panels=private_view.panels if private_view else (),
            panel_labels=private_view.panel_labels if private_view else (),
        )
        if private_view is None:
            self.clear_items()
        self.context = context
        self.private_view = private_view
        self.result = None
        self.post = None
        self.posted = False
        self.lock = asyncio.Lock()
        button = discord.ui.Button(label=AGENT_PRIVATE_CHECK_BUTTON, style=discord.ButtonStyle.secondary)
        button.callback = self.check_privately
        self.add_item(button)

    async def check_privately(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(ACTION_RESULT_OWNER, ephemeral=True)
            return
        if self.expired:
            await interaction.response.send_message(ACTION_RESULT_EXPIRED, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            try:
                if self.expired:
                    await interaction.followup.send(ACTION_RESULT_EXPIRED, ephemeral=True)
                    return
                if self.posted:
                    await interaction.followup.send(ACTION_RESULT_EXPIRED, ephemeral=True)
                    return
                await require_evidence_access(self.context)
                if self.result is None:
                    self.result = await self.context.state.private_answer_run()
                answer, attachments, private = self.result
                await assert_answer_scope(private, answer)
            except AgentAccessLost:
                await interaction.followup.send(ACTION_RESULT_EXPIRED, ephemeral=True)
                return
            except AgentUnavailableError:
                await interaction.followup.send(FAILURE_MESSAGE, ephemeral=True)
                return
            except Exception:
                LOGGER.exception("Agent private check failed: request=%s", self.context.source_message.id)
                await interaction.followup.send("The private check did not finish.", ephemeral=True)
                return
            files = attachment_files(attachments)
            post_view = (PostAnswerView(self) if self.post is not None
                         and ACCESS_VIEW_AUDIT_LOG not in private.state.required_access else None)
            try:
                image = next((item for item in attachments if image_attachment(item)), None)
                for index, (part, _) in enumerate(outgoing_parts(answer, attachment=image) or (("", False),)):
                    sent = await interaction.followup.send(
                        part or None, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
                        **({"files": files} if index == 0 and files else {}),
                        **({"view": post_view} if index == 0 and post_view is not None else {}),
                    )
                    if index == 0 and post_view is not None:
                        post_view.message = sent
            finally:
                for file in files:
                    file.close()

    async def on_timeout(self):
        async with self.lock:
            if not self.posted:
                await super().on_timeout()


class PostAnswerView(discord.ui.View):
    """An explicit disclosure of this private answer, shared across repeat checks."""

    def __init__(self, answer_view):
        super().__init__(timeout=PRIVATE_RESULT_TIMEOUT)
        self.answer_view = answer_view
        self.message = None
        self.expired = False
        button = discord.ui.Button(label=ACTION_POST_HERE_BUTTON, style=discord.ButtonStyle.secondary)
        button.callback = self.post_here
        self.add_item(button)

    async def post_here(self, interaction):
        parent = self.answer_view
        if interaction.user.id != parent.owner_id:
            await interaction.response.send_message(ACTION_RESULT_OWNER, ephemeral=True)
            return
        await interaction.response.defer()
        async with parent.lock:
            if self.expired or parent.expired or parent.posted or parent.post is None:
                await interaction.followup.send(ACTION_RESULT_EXPIRED, ephemeral=True)
                return
            try:
                answer, attachments, private = parent.result
                await assert_answer_scope(private, answer)
                if ACCESS_VIEW_AUDIT_LOG in private.state.required_access:
                    await interaction.followup.send(ACTION_UNAVAILABLE, ephemeral=True)
                    return
                # A failed transport acknowledgement may follow a completed post.
                # Do not offer a second send of an uncertain operation.
                parent.posted = True
                await parent.post(answer, attachments, private)
            except AgentAccessLost:
                await interaction.followup.send(ACTION_RESULT_EXPIRED, ephemeral=True)
                return
            except Exception:
                LOGGER.exception("Agent answer post failed: request=%s", parent.context.source_message.id)
                await interaction.followup.send("I couldn't confirm that the answer was posted.", ephemeral=True)
                return
            parent.posted = True
            log_scope_override(parent.context, private, parent.message.id)
            self.expired = True
            for item in self.children:
                item.disabled = True
            self.stop()
            try:
                await interaction.edit_original_response(view=self)
            except discord.DiscordException as error:
                log_cleanup_failure(LOGGER, "Agent post control could not be disabled", error)

    async def on_timeout(self):
        self.expired = True
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.DiscordException as error:
                log_cleanup_failure(LOGGER, "Agent post control could not be disabled", error)
