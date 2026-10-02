"""Recruitment message previews use the text posted by the feature."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from elbow_helper.features.agent.capabilities.recruitment.commands import (
    prepare_recstatement, run_recstatement,
)
from elbow_helper.features.recruitment.commands import RecruitmentCommandMixin


class RecruitmentStatementCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_template_is_previewed_before_posting(self):
        workflow = RecruitmentCommandMixin()
        applicant = SimpleNamespace(id=7, mention="<@7>", display_name="Applicant")
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 9
        channel.mention = "<#9>"
        channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True, send_messages=True,
        )
        channel.send = AsyncMock()
        guild = SimpleNamespace(
            id=1, me=SimpleNamespace(id=3),
            get_member=lambda member_id: applicant if member_id == 7 else None,
            get_channel_or_thread=lambda _: channel,
        )
        channel.guild = guild
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow), guild=guild,
            member=SimpleNamespace(id=4),
            source_message=SimpleNamespace(channel=channel),
        )
        for template in ("under16", "hyperactive"):
            with self.subTest(template=template):
                values = {"message": template, "applicant": 7,
                          "additional_notes": "Please reply here."}
                preview = await prepare_recstatement(context, values)
                self.assertTrue(await preview.recheck())
                self.assertIn("(blank line)", preview.lines)
                count = channel.send.await_count
                outcome = await run_recstatement(context, values)
                self.assertEqual(outcome.visibility, "private")
                self.assertEqual(channel.send.await_count, count + 1)
                self.assertIn("**Additional Notes:** Please reply here.",
                              channel.send.await_args.args[0])
