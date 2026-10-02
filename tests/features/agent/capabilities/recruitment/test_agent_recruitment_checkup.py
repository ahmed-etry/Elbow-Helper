"""Recruitment checkup previews the message that the feature posts."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.capabilities.recruitment.commands import (
    prepare_checkup, recruitment_adapters,
)
from elbow_helper.features.recruitment.commands import RecruitmentCommandMixin


class CheckupCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkup_posts_the_previewed_message_once(self):
        workflow = RecruitmentCommandMixin()
        bot_member = SimpleNamespace(id=1, top_role=SimpleNamespace(position=10))
        requester = SimpleNamespace(id=2)
        applicant = SimpleNamespace(
            id=3, mention="<@3>", display_name="Applicant", roles=[],
            top_role=SimpleNamespace(position=1),
        )
        channel = SimpleNamespace(
            id=4, mention="<#4>", send=AsyncMock(),
            permissions_for=lambda _: SimpleNamespace(
                view_channel=True, send_messages=True,
            ),
        )
        guild = SimpleNamespace(
            id=5, me=bot_member,
            get_member=lambda member_id: applicant if member_id == 3 else None,
            fetch_member=AsyncMock(return_value=applicant),
            get_channel_or_thread=lambda channel_id: channel if channel_id == 4 else None,
        )
        channel.guild = guild
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=guild, member=requester,
            source_message=SimpleNamespace(channel=channel),
        )
        values = {"applicant": 3, "account_linked": False}
        with patch("elbow_helper.features.recruitment.commands.discord.TextChannel",
                   new=SimpleNamespace):
            prepared = await prepare_checkup(context, values)
            self.assertTrue(await prepared.preview.recheck())
            self.assertIn("Post this recruitment checkup in <#4>.",
                          prepared.preview.lines)
            self.assertTrue(any("<@3>" in line for line in prepared.preview.lines))
            self.assertIs(next(adapter for adapter in recruitment_adapters()
                               if adapter.path == "/checkup").classification,
                          ActionClass.CHANGE)
            channel.send.assert_not_awaited()
            result = await prepared.run()
        self.assertEqual(result.result["channel_id"], 4)
        channel.send.assert_awaited_once()
        self.assertIn("<@3>", channel.send.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
