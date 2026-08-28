"""Recruiter review helpers for suggested links."""

from __future__ import annotations

import logging

import discord
from elbow_helper.discord.interactions import send_bound_view
from elbow_helper.discord.interactions import succeed
from elbow_helper.configuration.roles import CORE
from elbow_helper.configuration.roles import RECRUITERS

from .config import REVIEW_CHANNEL_ID
from .views import IgnoreSuggestionView
from .views import SuggestionCorrectionView
from .views import SuggestionReviewView


LOGGER = logging.getLogger(__name__)


class AccountLinksReviewMixin:
    def _can_review_links(self, member: discord.abc.User | discord.Member) -> bool:
        roles = getattr(member, "roles", [])
        return any(getattr(role, "id", None) in (CORE | RECRUITERS) for role in roles)

    def _build_suggestion_embed(self, suggestion: dict[str, object]) -> discord.Embed:
        player_name = str(suggestion.get("player_name") or suggestion.get("player_tag") or "Unknown")
        player_tag = str(suggestion.get("player_tag") or "")
        clan_code = str(suggestion.get("current_clan_code") or "?")
        proposed_user_id = int(suggestion.get("proposed_discord_user_id") or 0)
        if proposed_user_id:
            embed = discord.Embed(
                title="Possible Account Match",
                description=(
                    f"Is **{player_name}** (`{player_tag}`) in **{clan_code}** one of "
                    f"<@{proposed_user_id}>'s accounts?"
                ),
                color=discord.Color.orange(),
            )
        else:
            embed = discord.Embed(
                title="Clan Account Needs a Discord Link",
                description=(
                    f"**{player_name}** (`{player_tag}`) is in **{clan_code}**, but I couldn't "
                    "identify one Discord member to link it to."
                ),
                color=discord.Color.red(),
            )
        embed.add_field(name="In-Game Name", value=player_name, inline=False)
        embed.add_field(name="Player Tag", value=player_tag, inline=True)
        embed.add_field(name="Current Clan", value=clan_code, inline=True)
        if proposed_user_id:
            embed.add_field(name="Suggested Discord Member", value=f"<@{proposed_user_id}>", inline=False)
        return embed

    async def _get_review_channel(self) -> discord.TextChannel | None:
        channel = self.bot.get_channel(REVIEW_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            return channel
        try:
            fetched = await self.bot.fetch_channel(REVIEW_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.TextChannel) else None

    async def publish_suggestion(self, suggestion: dict[str, object]) -> None:
        channel = await self._get_review_channel()
        if channel is None:
            LOGGER.warning("Recruiter review channel not found: %s", REVIEW_CHANNEL_ID)
            return
        player_tag = str(suggestion.get("player_tag") or "")
        proposed_user_id = int(suggestion.get("proposed_discord_user_id") or 0)
        view = SuggestionReviewView(self, player_tag, has_candidate=bool(proposed_user_id))
        message = await channel.send(embed=self._build_suggestion_embed(suggestion), view=view)
        self.upsert_suggestion(
            player_tag=player_tag,
            player_name=str(suggestion.get("player_name") or ""),
            current_clan_code=str(suggestion.get("current_clan_code") or ""),
            current_clan_tag=str(suggestion.get("current_clan_tag") or ""),
            proposed_discord_user_id=int(suggestion.get("proposed_discord_user_id") or 0),
            proposed_display_name=str(suggestion.get("proposed_display_name") or ""),
            review_channel_id=channel.id,
            review_message_id=message.id,
        )
        self.bot.add_view(view, message_id=message.id)

    async def register_pending_suggestion_views(self) -> None:
        for suggestion in self.list_pending_suggestions():
            message_id = int(suggestion.get("review_message_id") or 0)
            if not message_id:
                continue
            proposed_user_id = int(suggestion.get("proposed_discord_user_id") or 0)
            self.bot.add_view(
                SuggestionReviewView(
                    self,
                    str(suggestion.get("player_tag") or ""),
                    has_candidate=bool(proposed_user_id),
                ),
                message_id=message_id,
            )

    async def _resolve_review_message(self, suggestion: dict[str, object]) -> discord.Message | None:
        channel_id = int(suggestion.get("review_channel_id") or 0)
        message_id = int(suggestion.get("review_message_id") or 0)
        if not channel_id or not message_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        if not isinstance(channel, discord.TextChannel):
            return None
        try:
            return await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def _finalize_suggestion_message(self, suggestion: dict[str, object]) -> None:
        message = await self._resolve_review_message(suggestion)
        if message is None:
            return
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.debug("Failed to finalize suggestion message for %s", suggestion.get("player_tag"))

    async def confirm_suggestion(self, interaction: discord.Interaction, player_tag: str) -> None:
        if not self._can_review_links(interaction.user):
            await interaction.response.send_message("You don't have permission to review player links.", ephemeral=True)
            return
        suggestion = self.get_pending_suggestion(player_tag)
        if not suggestion:
            await interaction.response.send_message("That suggestion is no longer pending.", ephemeral=True)
            return
        proposed_user_id = int(suggestion.get("proposed_discord_user_id") or 0)
        if not proposed_user_id:
            await interaction.response.send_message(
                "No Discord member was suggested. Use **Choose Member** instead.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.upsert_link(
            player_tag=player_tag,
            discord_user_id=proposed_user_id,
            player_name_last_seen=str(suggestion.get("player_name") or ""),
            last_seen_clan_tag=str(suggestion.get("current_clan_tag") or ""),
            last_seen_clan_code=str(suggestion.get("current_clan_code") or ""),
            last_seen_role="",
        )
        self.delete_suggestion(player_tag)
        await self._finalize_suggestion_message(suggestion)
        await self.refresh_board_for_clan(str(suggestion.get("current_clan_code") or ""))
        await succeed(interaction, "Link confirmed.")

    async def open_correction_flow(self, interaction: discord.Interaction, player_tag: str) -> None:
        if not self._can_review_links(interaction.user):
            await interaction.response.send_message("You don't have permission to review player links.", ephemeral=True)
            return
        suggestion = self.get_pending_suggestion(player_tag)
        if not suggestion:
            await interaction.response.send_message("That suggestion is no longer pending.", ephemeral=True)
            return
        view = SuggestionCorrectionView(self, player_tag)
        await send_bound_view(
            interaction,
            view=view,
            content=f"Choose the Discord member who owns `{player_tag}`.",
            ephemeral=True,
        )

    async def open_ignore_confirmation(self, interaction: discord.Interaction, player_tag: str) -> None:
        if not self._can_review_links(interaction.user):
            await interaction.response.send_message("You don't have permission to review player links.", ephemeral=True)
            return
        suggestion = self.get_pending_suggestion(player_tag)
        if not suggestion:
            await interaction.response.send_message("That suggestion is no longer pending.", ephemeral=True)
            return
        view = IgnoreSuggestionView(self, player_tag)
        await send_bound_view(
            interaction,
            view=view,
            content=(
                f"`{player_tag}` will no longer be suggested as an account match. "
                "This is permanent. Continue?"
            ),
            ephemeral=True,
        )

    async def correct_suggestion(self, interaction: discord.Interaction, player_tag: str, discord_user_id: int) -> None:
        if not self._can_review_links(interaction.user):
            await interaction.response.send_message("You don't have permission to review player links.", ephemeral=True)
            return
        suggestion = self.get_pending_suggestion(player_tag)
        if not suggestion:
            await interaction.response.send_message("That suggestion is no longer pending.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.upsert_link(
            player_tag=player_tag,
            discord_user_id=discord_user_id,
            player_name_last_seen=str(suggestion.get("player_name") or ""),
            last_seen_clan_tag=str(suggestion.get("current_clan_tag") or ""),
            last_seen_clan_code=str(suggestion.get("current_clan_code") or ""),
            last_seen_role="",
        )
        self.delete_suggestion(player_tag)
        await self._finalize_suggestion_message(suggestion)
        await self.refresh_board_for_clan(str(suggestion.get("current_clan_code") or ""))
        await succeed(interaction, "Corrected link saved.")

    async def ignore_suggestion(self, interaction: discord.Interaction, player_tag: str) -> None:
        if not self._can_review_links(interaction.user):
            await interaction.response.send_message("You don't have permission to review player links.", ephemeral=True)
            return
        suggestion = self.get_pending_suggestion(player_tag)
        if not suggestion:
            await interaction.response.send_message("That suggestion is no longer pending.", ephemeral=True)
            return
        self.add_ignored_tag(player_tag)
        self.delete_suggestion(player_tag)
        await self._finalize_suggestion_message(suggestion)
        await interaction.response.edit_message(content=f"`{player_tag}` will no longer be suggested as an account match.", view=None)
