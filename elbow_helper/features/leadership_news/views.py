from __future__ import annotations

import logging
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
        cog = interaction.client.get_cog("LeadNews")
        try:
            if cog and interaction.message and (
                    cog.public_news_prompt_source(interaction.message) == self.source_message_id):
                if await cog.dismiss_public_news_prompt(interaction.message):
                    return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.debug("Failed to delete interaction prompt message")
        try:
            channel = interaction.client.get_channel(self.prompt_channel_id)
            if cog and channel:
                message = await channel.fetch_message(self.prompt_message_id)
                await cog.dismiss_public_news_prompt(message)
                return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.debug("Failed to delete fallback prompt message %s", self.prompt_message_id)

    async def on_timeout(self) -> None:
        cog = self.bot.get_cog("LeadNews")
        try:
            channel = self.bot.get_channel(self.prompt_channel_id)
            if cog and channel:
                message = await channel.fetch_message(self.prompt_message_id)
                await cog.dismiss_public_news_prompt(message)
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

        target_channel: Optional[discord.TextChannel] = interaction.client.get_channel(PUBLIC_NEWS)
        if not target_channel:
            await interaction.response.send_message("The public news channel hasn't been set up. Check the lead news setup.", ephemeral=True)
            return

        try:
            cog = interaction.client.get_cog("LeadNews")
            if cog is None:
                await interaction.response.send_message(
                    "I couldn't publish that post. Try again in a moment.", ephemeral=True,
                )
                return
            await cog.publish_public_news(source_message, target_channel)
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

