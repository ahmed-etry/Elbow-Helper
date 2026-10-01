"""Support ticket previews carry the generated welcome into one confirmed run."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.configuration.channels import SUPPORT_TICKET_CATEGORY
from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.commands.adapters.support_tickets import (
    prepare_support_open, support_ticket_adapters,
)
from elbow_helper.features.support_tickets.commands import SupportCommandMixin


class SupportOpenCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_welcome_is_generated_once_and_posted_after_confirmation(self):
        member = MagicMock()
        member.id = 7
        member.mention = "<@7>"
        member.display_name = "Member"
        member.name = "member"
        member.top_role.position = 1
        bot_member = MagicMock()
        bot_member.id = 3
        bot_member.top_role.position = 10
        actor = SimpleNamespace(
            id=4, display_name="Lead",
            display_avatar=SimpleNamespace(url="https://example.com/avatar.png"),
        )
        category = SimpleNamespace(id=SUPPORT_TICKET_CATEGORY,
                                   mention=f"<#{SUPPORT_TICKET_CATEGORY}>")
        channel = SimpleNamespace(
            id=9, mention="<#9>", edit=AsyncMock(), send=AsyncMock(),
            delete=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=1, me=bot_member, default_role=MagicMock(id=1),
            get_member=lambda member_id: member if member_id == 7 else None,
            get_role=lambda _: None,
            get_channel=lambda channel_id: category if channel_id == SUPPORT_TICKET_CATEGORY else None,
            create_text_channel=AsyncMock(return_value=channel),
        )
        workflow = SupportCommandMixin()
        workflow.bot = SimpleNamespace(user=SimpleNamespace(id=3))
        workflow.welcome_messages = SimpleNamespace(
            create=AsyncMock(return_value="Welcome to this ticket."),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=guild, member=actor,
        )
        saved = {}

        def save(value):
            saved.clear()
            saved.update(value)

        with (
            patch("elbow_helper.features.support_tickets.commands.load_tickets",
                  side_effect=lambda: dict(saved)),
            patch("elbow_helper.features.support_tickets.commands.save_tickets",
                  side_effect=save),
        ):
            prepared = await prepare_support_open(
                context, {"user": 7, "topic": "War discussion"},
            )
            self.assertTrue(await prepared.preview.recheck())
            self.assertIn("Welcome to this ticket.", prepared.preview.lines)
            guild.create_text_channel.assert_not_awaited()
            self.assertIs(support_ticket_adapters()[0].classification,
                          ActionClass.CHANGE)
            result = await prepared.run()
            self.assertEqual(result.result["channel_id"], 9)
            self.assertEqual(saved["9"]["owner"], 7)
            self.assertIn("Welcome to this ticket.",
                          channel.send.await_args.kwargs["content"])
            workflow.welcome_messages.create.assert_awaited_once()
            channel.delete.assert_not_awaited()
