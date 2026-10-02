"""Support close previews the ticket, owner lock, and transcript destination."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.capabilities.support_tickets.commands import (
    prepare_support_close, support_ticket_adapters,
)


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


if __name__ == "__main__":
    unittest.main()
