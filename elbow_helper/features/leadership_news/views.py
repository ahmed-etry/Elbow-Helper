from __future__ import annotations

import logging
import re
from typing import Optional

import discord
from discord.ext import commands
from elbow_helper.discord.interactions import deny
from elbow_helper.discord.views import BaseTimeoutView

from elbow_helper.configuration.channels import PUBLIC_NEWS
from elbow_helper.configuration.roles import LEAD

LOGGER = logging.getLogger(__name__)


def has_any_role(member: discord.Member, role_ids: set[int] | frozenset[int]) -> bool:
    return any(role.id in role_ids for role in getattr(member, "roles", []))


class ForwardView(BaseTimeoutView):
    def __init__(
        self,
        bot: commands.Bot,
        source_message_id: int,
        prompt_message_id: int,
        prompt_channel_id: int,
        *,
        timeout: Optional[int] = None,
    ):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.source_message_id = source_message_id
        self.prompt_message_id = prompt_message_id
        self.prompt_channel_id = prompt_channel_id

    async def _delete_prompt(self, interaction: discord.Interaction):
        try:
            if interaction.message:
                await interaction.message.delete()
                return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.debug("Failed to delete interaction prompt message")
        try:
            channel = interaction.client.get_channel(self.prompt_channel_id)
            if channel:
                message = await channel.fetch_message(self.prompt_message_id)
                await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.debug("Failed to delete fallback prompt message %s", self.prompt_message_id)

    async def on_timeout(self) -> None:
        try:
            channel = self.bot.get_channel(self.prompt_channel_id)
            if channel:
                message = await channel.fetch_message(self.prompt_message_id)
                await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.debug("Prompt already unavailable on timeout: %s", self.prompt_message_id)

    @discord.ui.button(label="Publish", style=discord.ButtonStyle.primary, custom_id="lead_news:publish")
    async def publish_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_any_role(interaction.user, LEAD):
            await deny(interaction)
            return

        try:
            source_message = await interaction.channel.fetch_message(self.source_message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                "I couldn't load the original message for that post.",
                ephemeral=True,
            )
            return

        content = source_message.content or ""
        content = re.sub(r"<@&\d+>", "", content).strip()
        files = []
        if source_message.attachments:
            for attachment in source_message.attachments[:3]:
                try:
                    files.append(await attachment.to_file())
                except discord.HTTPException:
                    LOGGER.debug("Failed converting attachment %s", attachment.id)
                    continue

        target_channel: Optional[discord.TextChannel] = interaction.client.get_channel(PUBLIC_NEWS)
        if not target_channel:
            await interaction.response.send_message("The public news channel hasn't been set up. Check the lead news setup.", ephemeral=True)
            return

        try:
            await target_channel.send(
                content=content or None,
                files=files or None,
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            )
            await interaction.response.send_message(f"Published to <#{PUBLIC_NEWS}>", ephemeral=True)
            await self._delete_prompt(interaction)
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Failed publishing message %s", self.source_message_id)
            await interaction.response.send_message(
                "I couldn't publish that post. Try again in a moment.",
                ephemeral=True,
            )

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary, custom_id="lead_news:dismiss")
    async def dismiss_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_any_role(interaction.user, LEAD):
            await deny(interaction)
            return
        await interaction.response.send_message("The update was not published.", ephemeral=True)
        await self._delete_prompt(interaction)

