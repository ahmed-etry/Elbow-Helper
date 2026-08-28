"""Views used by recruitment workflows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord
from elbow_helper.discord.views import BaseTimeoutView


if TYPE_CHECKING:
    from .cog import Recruitment


class PersistentEndNowView(BaseTimeoutView):
    def __init__(self, guild_id: int, ticket_channel_id: int, applicant_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.ticket_channel_id = ticket_channel_id
        self.applicant_id = applicant_id

        open_button = discord.ui.Button(
            label="Open Ticket",
            style=discord.ButtonStyle.link,
            url=f"https://discord.com/channels/{guild_id}/{ticket_channel_id}",
        )
        self.add_item(open_button)

        end_button = discord.ui.Button(
            label="End Now",
            style=discord.ButtonStyle.danger,
            custom_id=f"trial_end_now_{ticket_channel_id}_{applicant_id}",
        )
        end_button.callback = self.end_now_callback
        self.add_item(end_button)

    async def end_now_callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Recruitment")
        if not cog:
            await interaction.response.send_message("Recruitment tools aren't available.", ephemeral=True)
            return
        ok = await cog.end_trial_now(interaction, self.ticket_channel_id, self.applicant_id)
        if ok:
            try:
                await interaction.message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class PersistentEndTrialView(BaseTimeoutView):
    def __init__(self, ticket_channel_id: int, applicant_id: int):
        super().__init__(timeout=None)
        self.ticket_channel_id = ticket_channel_id
        self.applicant_id = applicant_id

        button = discord.ui.Button(
            label="End Trial",
            style=discord.ButtonStyle.success,
            custom_id=f"trial_end_{ticket_channel_id}_{applicant_id}",
        )
        button.callback = self.end_trial_callback
        self.add_item(button)

    async def end_trial_callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Recruitment")
        if not cog:
            await interaction.response.send_message("Recruitment tools aren't available.", ephemeral=True)
            return
        reminder_info = await cog._get_trial_reminder_entry(self.ticket_channel_id)
        if reminder_info and reminder_info.get("resolved_at"):
            await interaction.response.send_message("This reminder has already been handled.", ephemeral=True)
            return

        ok = await cog.end_trial_now(
            interaction,
            self.ticket_channel_id,
            self.applicant_id,
            allow_missing=True,
            show_success_confirmation=False,
        )
        if ok:
            await cog._mark_trial_reminder_resolved(
                ticket_channel_id=self.ticket_channel_id,
                applicant_id=self.applicant_id,
                resolver_id=interaction.user.id,
                message=interaction.message if isinstance(interaction.message, discord.Message) else None,
            )


class AcceptConfirmationView(BaseTimeoutView):
    def __init__(
        self,
        cog: "Recruitment",
        *,
        payload: dict[str, Any],
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.payload = payload

        confirm_button = discord.ui.Button(
            label="Confirm",
            style=discord.ButtonStyle.success,
        )
        confirm_button.callback = self._confirm
        self.add_item(confirm_button)

        cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
        )
        cancel_button.callback = self._cancel
        self.add_item(cancel_button)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=None)
        await self.cog.complete_accept_confirmation(interaction, self.payload)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Acceptance cancelled. No changes were made.", embed=None, view=None)
