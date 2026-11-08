from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import discord

from elbow_helper.configuration.channels import RECRUITMENT_TICKET_CATEGORY
from elbow_helper.configuration.roles import CORE
from elbow_helper.features.recruitment.commands import RecruitmentCommandMixin


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


if __name__ == "__main__":
    unittest.main()
