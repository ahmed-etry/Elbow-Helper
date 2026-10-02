from __future__ import annotations
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.capabilities.support_tickets.commands import (
    prepare_support_close, support_ticket_adapters,
)
from unittest.mock import AsyncMock, MagicMock, patch
from elbow_helper.configuration.channels import SUPPORT_TICKET_CATEGORY
from elbow_helper.features.agent.capabilities.support_tickets.commands import (
    prepare_support_open, support_ticket_adapters,
)
from elbow_helper.features.support_tickets.commands import SupportCommandMixin


class SupportCloseCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_previews_history_and_uses_public_feature_operation(self):
        bot_member = SimpleNamespace(
            id=1, top_role=SimpleNamespace(position=10),
        )
        requester = SimpleNamespace(id=2, mention="<@2>")
        owner = SimpleNamespace(
            id=3, mention="<@3>", top_role=SimpleNamespace(position=1),
        )
        guild = SimpleNamespace(id=5, me=bot_member, filesize_limit=1000)

        def permissions(_):
            return SimpleNamespace(view_channel=True, send_messages=True)

        channel = SimpleNamespace(
            id=4, mention="<#4>", guild=guild,
            permissions_for=permissions,
        )
        log_channel = SimpleNamespace(
            id=9, mention="<#9>", guild=guild,
            permissions_for=permissions,
        )
        guild.get_channel_or_thread = lambda channel_id: (
            channel if channel_id == 4 else log_channel if channel_id == 9 else None
        )
        prepared = {
            "issue": None, "guild": guild, "channel": channel,
            "actor": requester, "ticket_info": {"source": "open"},
            "owner": owner, "source": "open", "log_channel_id": 9,
            "log_channel": log_channel,
            "transcript_filename": "transcript-support.html",
        }
        history = ((10, 3, "I need help.", None, (), (), ()),)
        workflow = SimpleNamespace(
            prepare_support_close=lambda *_: prepared,
            support_close_history=AsyncMock(return_value=history),
            close_support_ticket=AsyncMock(return_value={
                "status": "complete", "log_channel_id": 9,
                "log_message_id": 88, "transcript_uploaded": True,
            }),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=guild, member=requester,
            source_message=SimpleNamespace(channel=channel),
        )
        change = await prepare_support_close(context, {})
        self.assertTrue(await change.preview.recheck())
        self.assertIn("Stop <@3> from sending in the ticket.",
                      change.preview.lines)
        self.assertIn("I need help.", change.preview.lines)
        self.assertIn("Post the ticket log in <#9>.", change.preview.lines)
        workflow.close_support_ticket.assert_not_awaited()
        result = await change.run()
        self.assertEqual(result.after["log_message_id"], 88)
        self.assertIs(next(adapter for adapter in support_ticket_adapters()
                           if adapter.path == "/close").classification,
                      ActionClass.CHANGE)


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
