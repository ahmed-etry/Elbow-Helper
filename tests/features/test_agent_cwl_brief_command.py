"""CWL briefs preview the feature's full message before posting."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.commands.adapters.cwl_brief import (
    prepare_cwl_brief, run_cwl_brief,
)
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.cwl.announcements import CwlAnnouncementMixin


class CwlBriefCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_previews_feature_content_and_posts_once(self):
        channel = SimpleNamespace(id=7, mention="#cwl-info")
        channel.permissions_for = lambda _: SimpleNamespace(
            view_channel=True, send_messages=True,
        )
        workflow = CwlAnnouncementMixin()
        workflow.bot = SimpleNamespace(get_channel=lambda _: channel)
        workflow._send_chunked = AsyncMock()
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=SimpleNamespace(me=SimpleNamespace(id=2)),
            member=SimpleNamespace(id=3), state=AgentTurnState(),
        )
        values = {
            "clan": "BEH", "mode": "highly_motivated",
            "helper_cwl": "@helper", "rotations": True,
        }
        preview = await prepare_cwl_brief(context, values)
        self.assertIn("#cwl-info", preview.lines[0])
        self.assertGreater(len(preview.lines), 1)
        self.assertTrue(await preview.recheck())
        workflow._send_chunked.assert_not_awaited()
        outcome = await run_cwl_brief(context, values)
        self.assertEqual(outcome.status, "complete")
        workflow._send_chunked.assert_awaited_once()
        self.assertEqual(workflow._send_chunked.await_args.args[0], channel)
