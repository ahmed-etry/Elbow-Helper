from __future__ import annotations

import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import discord

from elbow_helper.features.examination.intake.view import ExaminationPromoIntakeMixin


class ExaminationPromoIntakeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _owner(ticket_channel: discord.TextChannel) -> ExaminationPromoIntakeMixin:
        owner = object.__new__(ExaminationPromoIntakeMixin)
        owner.bot = MagicMock()
        owner.bot.get_channel.return_value = ticket_channel
        owner._pending_ticket_retries = {}
        owner._pending_ticket_notified = set()
        owner._pending_ticket_failed = set()
        owner._retire_routing_message_for_route_change = AsyncMock()
        owner._retire_availability_prompt_for_route_change = AsyncMock()
        owner._render_promo_intake_message = AsyncMock()
        owner._save = MagicMock()
        return owner

    async def test_applicant_route_change_uses_current_intake_renderer_contract(self) -> None:
        ticket_channel = MagicMock(spec=discord.TextChannel)
        ticket_channel.id = 456
        owner = self._owner(ticket_channel)
        owner._can_change_promo_route = MagicMock(return_value=True)
        owner._reset_case_for_route_change = MagicMock()

        case = {
            "type": "clan_promo",
            "ticket_channel_id": ticket_channel.id,
        }
        interaction = MagicMock()
        interaction.channel_id = ticket_channel.id
        interaction.user.id = 123
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        await owner._execute_promo_change_route(interaction, case=case)

        owner._render_promo_intake_message.assert_awaited_once_with(
            ticket_channel,
            case,
        )
        interaction.followup.send.assert_awaited_once_with(
            "The promotion questions are open again in the ticket, and this review is closed.",
            ephemeral=True,
        )

    async def test_leadership_route_change_uses_current_intake_renderer_contract(self) -> None:
        ticket_channel = MagicMock(spec=discord.TextChannel)
        ticket_channel.id = 456
        owner = self._owner(ticket_channel)
        owner._has_exam_permissions = MagicMock(return_value=True)
        owner._get_routing_channel = AsyncMock(return_value=None)
        owner._reset_case_review_state = MagicMock()
        owner.route_ticket = AsyncMock()
        owner._update_promo_intake_message = AsyncMock()

        case = {
            "type": "clan_promo",
            "ticket_channel_id": ticket_channel.id,
        }
        owner._get_case = MagicMock(return_value=case)
        interaction = MagicMock()
        interaction.user = MagicMock(spec=discord.Member)
        interaction.user.id = 123
        interaction.response.edit_message = AsyncMock()
        interaction.followup.send = AsyncMock()

        await owner._execute_leadership_promo_route_change(
            interaction,
            ticket_channel_id=ticket_channel.id,
            routing_message_id=789,
            from_clan="BES",
            to_clan="BE4",
        )

        owner._render_promo_intake_message.assert_awaited_once_with(
            ticket_channel,
            case,
        )
        owner.route_ticket.assert_awaited_once_with(ticket_channel, "clan_promo")
        interaction.followup.send.assert_awaited_once_with(
            "Promotion request updated.",
            ephemeral=True,
        )


if __name__ == "__main__":
    unittest.main()
