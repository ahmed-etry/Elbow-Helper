from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import discord

from elbow_helper.configuration.channels import RECRUITMENT_TICKET_CATEGORY
from elbow_helper.configuration.roles import CORE
from elbow_helper.features.recruitment.commands import RecruitmentCommandMixin
from elbow_helper.data.events.testing import MemoryEventsStore
from elbow_helper.features.recruitment.ai import AIMixin
from elbow_helper.features.recruitment.state import RecruitmentState
from elbow_helper.features.recruitment.tickets import TicketMixin


class _TicketChannel:
    mention = "<#123>"

    def __init__(self, messages: list[SimpleNamespace]) -> None:
        self._messages = messages
        self.history_calls: list[dict[str, object]] = []

    def history(self, **kwargs: object):
        self.history_calls.append(kwargs)
        limit = kwargs["limit"]
        oldest_first = bool(kwargs["oldest_first"])
        selected = list(self._messages)
        if limit is not None:
            selected = (
                selected[: int(limit)]
                if oldest_first
                else list(reversed(selected[-int(limit) :]))
            )
        elif not oldest_first:
            selected.reverse()

        async def messages():
            for message in selected:
                yield message

        return messages()


class OpinionCommandTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _member(*role_ids: int) -> discord.Member:
        member = MagicMock(spec=discord.Member)
        member.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]
        return member

    @staticmethod
    def _channel(
        channel_id: int,
        name: str,
        category_id: int,
        *,
        visible: bool = True,
    ) -> discord.TextChannel:
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = channel_id
        channel.name = name
        channel.category_id = category_id
        channel.permissions_for.return_value = SimpleNamespace(
            view_channel=visible,
        )
        return channel

    async def test_ticket_autocomplete_only_shows_visible_applicant_tickets(
        self,
    ) -> None:
        member = self._member(next(iter(CORE)))
        matching = self._channel(
            101,
            "ticket-alex",
            RECRUITMENT_TICKET_CATEGORY,
        )
        hidden = self._channel(
            102,
            "ticket-avery",
            RECRUITMENT_TICKET_CATEGORY,
            visible=False,
        )
        other_category = self._channel(103, "staff-chat", 999)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(
                text_channels=[other_category, hidden, matching],
            ),
            user=member,
        )

        choices = await RecruitmentCommandMixin().opinion_ticket_autocomplete(
            interaction,
            "alex",
        )

        self.assertEqual(
            [(choice.name, choice.value) for choice in choices],
            [("#ticket-alex", "101")],
        )

    async def test_ticket_autocomplete_hides_results_from_other_members(
        self,
    ) -> None:
        ticket = self._channel(
            101,
            "ticket-alex",
            RECRUITMENT_TICKET_CATEGORY,
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(text_channels=[ticket]),
            user=self._member(),
        )

        choices = await RecruitmentCommandMixin().opinion_ticket_autocomplete(
            interaction,
            "",
        )

        self.assertEqual(choices, [])

    async def test_opinion_rejects_a_channel_outside_the_applicant_category(
        self,
    ) -> None:
        member = self._member(next(iter(CORE)))
        other_channel = self._channel(103, "staff-chat", 999)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(
                get_channel=MagicMock(return_value=other_channel),
            ),
            user=member,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        workflow = RecruitmentCommandMixin()
        workflow.build_ticket_second_opinion = AsyncMock()

        await RecruitmentCommandMixin.slash_opinion.callback(
            workflow,
            interaction,
            str(other_channel.id),
        )

        workflow.build_ticket_second_opinion.assert_not_awaited()
        interaction.followup.send.assert_awaited_once_with(
            "Choose an applicant ticket.",
            ephemeral=True,
        )


class RecruitmentCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_deletes_stay_saved_and_deleted_tickets_lose_reminder_entries(self):
        events = MemoryEventsStore()
        self.addCleanup(events.close)
        state = RecruitmentState(events)
        await state.startup()
        old = datetime.now(timezone.utc) - timedelta(days=3)
        forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied")
        missing = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")
        message = SimpleNamespace(delete=AsyncMock(side_effect=forbidden))
        room = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        bot = SimpleNamespace(
            get_channel=lambda channel_id: room if channel_id == 800 else (
                SimpleNamespace(id=channel_id) if channel_id == 200 else None),
            fetch_channel=AsyncMock(side_effect=missing),
        )
        tickets = TicketMixin()
        tickets.bot, tickets.logger, tickets.recruitment_state = bot, MagicMock(), state
        tickets._ticket_reminder_lock = asyncio.Lock()
        tickets.ticket_reminders = {
            ticket_id: {"last_reminder_at": old, "message_id": ticket_id + 500,
                        "message_channel_id": 800, "last_applicant_message_id": 9}
            for ticket_id in (200, 201)
        }
        await state.save_ticket_activity(tickets.ticket_reminders)
        await TicketMixin.cleanup_old_ticket_reminders.coro(tickets)
        self.assertTrue(all(info["message_id"] for info in tickets.ticket_reminders.values()))
        self.assertEqual(state.ticket_activity(), tickets.ticket_reminders)
        message.delete.side_effect = None
        await TicketMixin.cleanup_old_ticket_reminders.coro(tickets)
        self.assertEqual(set(tickets.ticket_reminders), {200})
        self.assertIsNone(tickets.ticket_reminders[200]["message_id"])
        self.assertEqual(tickets.ticket_reminders[200]["last_applicant_message_id"], 9)
        self.assertEqual(state.ticket_activity(), tickets.ticket_reminders)

    async def test_failed_ai_summary_deletes_are_done_on_the_next_cleanup(self):
        events = MemoryEventsStore()
        self.addCleanup(events.close)
        state = RecruitmentState(events)
        await state.startup()
        message = SimpleNamespace(delete=AsyncMock(side_effect=discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "denied")))
        owner = AIMixin()
        owner.bot = SimpleNamespace(get_channel=lambda _: SimpleNamespace(fetch_message=AsyncMock(return_value=message)))
        owner.logger, owner.recruitment_state = MagicMock(), state
        owner._applicant_ai_lock = asyncio.Lock()
        owner.applicant_ai_messages = {"900": {
            "channel_id": 800, "created_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
        }}
        await state.save_ai_messages(owner.applicant_ai_messages)
        await owner._cleanup_applicant_ai_messages()
        self.assertIn("900", owner.applicant_ai_messages)
        self.assertEqual(state.ai_messages(), owner.applicant_ai_messages)
        message.delete.side_effect = None
        await owner._cleanup_applicant_ai_messages()
        self.assertEqual(owner.applicant_ai_messages, {})
        self.assertEqual(state.ai_messages(), {})
        self.assertEqual(message.delete.await_count, 2)


if __name__ == "__main__":
    unittest.main()
