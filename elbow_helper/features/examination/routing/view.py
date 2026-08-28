"""Routing-message interaction view for examination cases."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional

import discord
from elbow_helper.discord.views import BaseTimeoutView

from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX, DEFAULT_THUMBNAIL_URL
from ..intake.logic import PROMO_SOURCES
from ..intake.logic import is_valid_route
from ..intake.logic import valid_targets_for_source

if TYPE_CHECKING:
    from ..cog import Examination


class LeadershipPromoRouteFromSelect(discord.ui.Select["LeadershipPromoRouteChangeView"]):
    def __init__(self, current: str | None = None) -> None:
        options = [
            discord.SelectOption(label=code, value=code, default=(code == current))
            for code in PROMO_SOURCES
        ]
        super().__init__(
            placeholder="Choose current clan",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.set_from_clan(interaction, self.values[0])


class LeadershipPromoRouteToSelect(discord.ui.Select["LeadershipPromoRouteChangeView"]):
    def __init__(self, from_clan: str, current: str | None = None) -> None:
        options = [
            discord.SelectOption(label=code, value=code, default=(code == current))
            for code in valid_targets_for_source(from_clan)
        ]
        super().__init__(
            placeholder="Choose promotion target",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.set_to_clan(interaction, self.values[0])


class LeadershipPromoRouteConfirmButton(discord.ui.Button["LeadershipPromoRouteChangeView"]):
    def __init__(self) -> None:
        super().__init__(label="Confirm Change", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.confirm(interaction)


class LeadershipPromoRouteCancelButton(discord.ui.Button["LeadershipPromoRouteChangeView"]):
    def __init__(self) -> None:
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.cancel(interaction)


class LeadershipPromoRouteChangeView(BaseTimeoutView):
    def __init__(
        self,
        cog: Examination,
        *,
        invoker_id: int,
        ticket_channel_id: int,
        routing_message_id: int,
        from_clan: str | None = None,
        to_clan: str | None = None,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.invoker_id = invoker_id
        self.ticket_channel_id = ticket_channel_id
        self.routing_message_id = routing_message_id
        self.from_clan = str(from_clan or "").upper() or None
        self.to_clan = str(to_clan or "").upper() or None
        self._build_items()

    def _build_items(self) -> None:
        self.clear_items()
        self.add_item(LeadershipPromoRouteFromSelect(self.from_clan))
        if self.from_clan:
            self.add_item(LeadershipPromoRouteToSelect(self.from_clan, self.to_clan))
        if self.from_clan and self.to_clan:
            self.add_item(LeadershipPromoRouteConfirmButton())
        self.add_item(LeadershipPromoRouteCancelButton())

    def _content(self) -> str:
        if not self.from_clan:
            return "Choose the correct current clan for this promotion."
        if not self.to_clan:
            return f"Current clan: **{self.from_clan}**\nChoose the correct promotion target."
        return f"Current clan: **{self.from_clan}**\nPromotion target: **{self.to_clan}**"

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.invoker_id:
            return True
        await interaction.response.send_message("Only the person who started this change can use these controls.", ephemeral=True)
        return False

    async def set_from_clan(self, interaction: discord.Interaction, value: str) -> None:
        if not await self._check_owner(interaction):
            return
        from_clan = str(value or "").upper()
        if from_clan not in PROMO_SOURCES:
            await interaction.response.send_message("That clan is not available for promotion changes.", ephemeral=True)
            return
        self.from_clan = from_clan
        self.to_clan = None
        self._build_items()
        await interaction.response.edit_message(content=self._content(), view=self)

    async def set_to_clan(self, interaction: discord.Interaction, value: str) -> None:
        if not await self._check_owner(interaction):
            return
        to_clan = str(value or "").upper()
        if not is_valid_route(self.from_clan, to_clan):
            await interaction.response.send_message("That promotion isn't available from the selected clan.", ephemeral=True)
            return
        self.to_clan = to_clan
        self._build_items()
        await interaction.response.edit_message(content=self._content(), view=self)

    async def confirm(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        if not self.from_clan or not self.to_clan or not is_valid_route(self.from_clan, self.to_clan):
            await interaction.response.send_message("Choose a valid current clan and promotion target first.", ephemeral=True)
            return
        await self.cog._execute_leadership_promo_route_change(
            interaction,
            ticket_channel_id=self.ticket_channel_id,
            routing_message_id=self.routing_message_id,
            from_clan=self.from_clan,
            to_clan=self.to_clan,
        )

    async def cancel(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        await interaction.response.edit_message(content="No changes made.", view=None)


class ExamRoutingView(BaseTimeoutView):
    def __init__(self, cog: Examination, *, ticket_type: str = "clan_promo", exam_required: bool = True):
        super().__init__(timeout=None)
        self.cog = cog
        if ticket_type == "elder_promo":
            for child in list(self.children):
                self.remove_item(child)
        elif not exam_required:
            for child in list(self.children):
                if isinstance(child, discord.ui.Button) and child.custom_id == "exam_route_overlap":
                    self.remove_item(child)
                if (
                    isinstance(child, discord.ui.Button)
                    and ticket_type == "clan_promo"
                    and child.custom_id == "exam_route_details"
                ):
                    self.remove_item(child)

    def _check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        return self.cog._has_exam_permissions(interaction.user)

    def _find_case(self, message_id: int) -> Optional[Dict[str, Any]]:
        cases = self.cog._get_cases()
        for case in cases.values():
            if case.get("routing_message_id") == message_id:
                return case
        return None

    @discord.ui.button(
        label="Check Availability",
        style=discord.ButtonStyle.secondary,
        custom_id="exam_route_overlap",
    )
    async def check_overlap(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check(interaction):
            await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "I couldn't load your member details. Please try again.",
                ephemeral=True,
            )
            return
        case = self._find_case(interaction.message.id)
        if not case:
            await interaction.response.send_message("This promotion request is no longer available.", ephemeral=True)
            return
        if case.get("exam_required") is False:
            await interaction.response.send_message(
                "This promotion doesn't require an exam.",
                ephemeral=True,
            )
            return
        availability_text = self.cog._format_applicant_availability(
            case.get("availability") or "",
            case.get("availability_structured") or case.get("availability_windows") or [],
        )
        embed = discord.Embed(
            title="Availability Overlap",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.add_field(name="Applicant Availability", value=availability_text, inline=False)
        embed.add_field(
            name="Your Overlap",
            value=self.cog._format_overlap_windows(
                self.cog._get_member_overlap_windows(case, interaction.user),
                limit=4,
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="View Details",
        style=discord.ButtonStyle.secondary,
        custom_id="exam_route_details",
    )
    async def view_details(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check(interaction):
            await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "I couldn't load your member details. Please try again.",
                ephemeral=True,
            )
            return
        case = self._find_case(interaction.message.id)
        if not case:
            await interaction.response.send_message("This promotion request is no longer available.", ephemeral=True)
            return
        if case.get("exam_required") is False:
            embed = discord.Embed(
                title="Promotion Details",
                color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
            embed.add_field(
                name="Promotion",
                value=case.get("route_summary") or "Not provided",
                inline=False,
            )
            embed.add_field(
                name="Town Hall",
                value=str(case.get("th_level")) if case.get("th_level") else "Not provided",
                inline=False,
            )
            embed.add_field(name="Review Team", value="Leadership review", inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        availability = case.get("availability") or ""
        applicant_windows = case.get("availability_structured") or case.get("availability_windows") or []
        availability_text = self.cog._format_applicant_availability(
            availability,
            applicant_windows,
        )
        availability_examples = self.cog._format_applicant_availability_examples(
            availability,
            applicant_windows,
        )
        embed = discord.Embed(
            title="Exam Details",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.add_field(name="Applicant Availability", value=availability_text, inline=False)
        if availability_examples:
            embed.add_field(name="Upcoming Available Times", value=availability_examples, inline=False)
        if self.cog._is_leadership_member(interaction.user):
            matched_members = self.cog._get_case_matched_members(case, interaction.guild)
            embed.add_field(
                name="Matched Examiners",
                value=self.cog._format_matched_examiners(matched_members),
                inline=False,
            )
            embed.add_field(
                name="Shared Availability by Examiner",
                value=self.cog._build_overlap_details_text(case, matched_members),
                inline=False,
            )
        else:
            embed.add_field(
                name="Your Overlap",
                value=self.cog._format_overlap_windows(
                    self.cog._get_member_overlap_windows(case, interaction.user),
                    limit=4,
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Change Promotion",
        style=discord.ButtonStyle.secondary,
        custom_id="exam_route_change_route",
    )
    async def change_route(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check(interaction):
            await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
            return
        if not interaction.message:
            await interaction.response.send_message("This review post is no longer available.", ephemeral=True)
            return
        case = self._find_case(interaction.message.id)
        if not case:
            await interaction.response.send_message("This promotion request is no longer available.", ephemeral=True)
            return
        if case.get("type") != "clan_promo":
            await interaction.response.send_message("Only clan promotion reviews can be changed here.", ephemeral=True)
            return
        ticket_channel_id = case.get("ticket_channel_id")
        if not ticket_channel_id:
            await interaction.response.send_message("The ticket channel is no longer available.", ephemeral=True)
            return
        view = LeadershipPromoRouteChangeView(
            self.cog,
            invoker_id=interaction.user.id,
            ticket_channel_id=int(ticket_channel_id),
            routing_message_id=interaction.message.id,
            from_clan=case.get("from_clan"),
            to_clan=case.get("to_clan"),
        )
        await interaction.response.send_message(
            view._content(),
            view=view,
            ephemeral=True,
        )
