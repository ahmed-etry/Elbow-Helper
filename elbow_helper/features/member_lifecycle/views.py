from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from elbow_helper.discord.interactions import send_bound_view
from elbow_helper.discord.pagination import format_page_footer
from elbow_helper.discord.pagination import NEXT_PAGE_LABEL
from elbow_helper.discord.pagination import PREV_PAGE_LABEL
from elbow_helper.discord.views import BaseTimeoutView

from elbow_helper.configuration.channels import OVERSEEING_TERRACE
from elbow_helper.configuration.guild import GUILD_ID
from .state import save_state

if TYPE_CHECKING:
    from .cog import MemberLifecycle

LOGGER = logging.getLogger(__name__)


class ApplicantCleanupSelect(discord.ui.Select):
    def __init__(self, view: "ApplicantCleanupSelectView", options: list[discord.SelectOption]):
        super().__init__(
            placeholder="Select applicants to remove from the server",
            min_values=0,
            max_values=len(options),
            options=options,
        )
        self._parent_view = view

    async def callback(self, interaction: discord.Interaction):
        self._parent_view.update_page_selection(self.values)
        await interaction.response.edit_message(content=self._parent_view.content(), view=self._parent_view)


class ApplicantCleanupSelectView(BaseTimeoutView):
    def __init__(self, cog: "MemberLifecycle", report_message_id: int, applicants: list[tuple[int, str]]):
        super().__init__(timeout=300)
        self.cog = cog
        self.report_message_id = report_message_id
        self.applicants = applicants
        self.page_size = 25
        self.page_index = 0
        self.selected_ids = {str(applicant_id) for applicant_id, _ in applicants}

        self.select = ApplicantCleanupSelect(self, self.page_options())
        self.add_item(self.select)

        self.prev_button = discord.ui.Button(style=discord.ButtonStyle.secondary, label=PREV_PAGE_LABEL, disabled=True)
        self.prev_button.callback = self.prev_page
        self.add_item(self.prev_button)

        self.next_button = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label=NEXT_PAGE_LABEL,
            disabled=self.page_count() <= 1,
        )
        self.next_button.callback = self.next_page
        self.add_item(self.next_button)

        self.confirm_button = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label=f"Confirm Removal ({len(self.selected_ids)})",
        )
        self.confirm_button.callback = self.confirm_kick
        self.add_item(self.confirm_button)

        cancel_button = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Cancel")
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

    def page_count(self) -> int:
        return max(1, (len(self.applicants) + self.page_size - 1) // self.page_size)

    def page_slice(self) -> list[tuple[int, str]]:
        start = self.page_index * self.page_size
        end = start + self.page_size
        return self.applicants[start:end]

    def page_options(self) -> list[discord.SelectOption]:
        options: list[discord.SelectOption] = []
        for applicant_id, name in self.page_slice():
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=str(applicant_id),
                    default=str(applicant_id) in self.selected_ids,
                )
            )
        return options

    def update_confirm_label(self) -> None:
        self.confirm_button.label = f"Confirm Removal ({len(self.selected_ids)})"

    def refresh_page(self) -> None:
        options = self.page_options()
        self.select.options = options
        self.select.max_values = len(options)
        self.prev_button.disabled = self.page_index <= 0
        self.next_button.disabled = self.page_index >= self.page_count() - 1
        self.update_confirm_label()

    def update_page_selection(self, values: list[str]) -> None:
        page_ids = {str(applicant_id) for applicant_id, _ in self.page_slice()}
        self.selected_ids.difference_update(page_ids)
        self.selected_ids.update(values)
        self.update_confirm_label()

    def content(self) -> str:
        return (
            "Select applicants to remove from the server. All are pre-selected; deselect anyone to keep.\n"
            f"{format_page_footer(self.page_index + 1, self.page_count())} • Selected: {len(self.selected_ids)}"
        )

    async def prev_page(self, interaction: discord.Interaction):
        if self.page_index > 0:
            self.page_index -= 1
            self.refresh_page()
        await interaction.response.edit_message(content=self.content(), view=self)

    async def next_page(self, interaction: discord.Interaction):
        if self.page_index < self.page_count() - 1:
            self.page_index += 1
            self.refresh_page()
        await interaction.response.edit_message(content=self.content(), view=self)

    async def cancel(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content="No applicants were removed.", view=None)

    async def confirm_kick(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message("You don't have permission to remove applicants from the server.", ephemeral=True)
            return
        if not self.selected_ids:
            await interaction.response.send_message("No applicants selected.", ephemeral=True)
            return

        guild = interaction.guild or self.cog.bot.get_guild(GUILD_ID)
        if not guild:
            await interaction.response.send_message(
                "I couldn't reach the server right now. Try again in a moment.",
                ephemeral=True,
            )
            return

        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except (discord.InteractionResponded, discord.NotFound):
            pass

        reason = f"Overdue applicant review started by {interaction.user}"
        for uid in sorted(self.selected_ids):
            member = guild.get_member(int(uid))
            if not member:
                continue
            try:
                await member.kick(reason=reason)
            except (discord.Forbidden, discord.HTTPException):
                continue

        report = self.cog.state.get("applicant_reports", {}).get(str(self.report_message_id))
        if report:
            report["active"] = False
            save_state(self.cog.state)

        try:
            channel = guild.get_channel(OVERSEEING_TERRACE)
            if channel and isinstance(channel, discord.TextChannel):
                message = await channel.fetch_message(self.report_message_id)
                await message.edit(view=ApplicantCleanupView(self.cog, self.report_message_id, disabled=True))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.debug("Failed to disable applicant cleanup view for %s", self.report_message_id)

        try:
            self.stop()
            await interaction.delete_original_response()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.debug("Failed to delete applicant cleanup selector message")


class ApplicantCleanupView(BaseTimeoutView):
    def __init__(self, cog: "MemberLifecycle", report_message_id: int, disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.report_message_id = report_message_id
        cleanup_button = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="Remove Applicants",
            custom_id=f"applicant_cleanup:{report_message_id}",
            disabled=disabled,
        )
        cleanup_button.callback = self.cleanup
        self.add_item(cleanup_button)

    async def cleanup(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message("You don't have permission to remove applicants from the server.", ephemeral=True)
            return

        report = self.cog.state.get("applicant_reports", {}).get(str(self.report_message_id))
        if not report or not report.get("active"):
            await interaction.response.send_message(
                "This report has expired. Open a new overdue-applicant report to continue.",
                ephemeral=True,
            )
            return

        guild = interaction.guild or self.cog.bot.get_guild(GUILD_ID)
        if not guild:
            await interaction.response.send_message(
                "I couldn't reach the server right now. Try again in a moment.",
                ephemeral=True,
            )
            return

        applicant_ids = report.get("applicant_ids", [])
        applicants: list[tuple[int, str]] = []
        for uid in applicant_ids:
            member = guild.get_member(int(uid))
            if not member:
                continue
            applicants.append((member.id, member.display_name))

        if not applicants:
            await interaction.response.send_message("No overdue applicants were found.", ephemeral=True)
            return

        view = ApplicantCleanupSelectView(self.cog, self.report_message_id, applicants)
        await send_bound_view(interaction, content=view.content(), view=view, ephemeral=True)
