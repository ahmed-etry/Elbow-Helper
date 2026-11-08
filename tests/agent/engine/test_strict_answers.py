"""Formatting requests still use answer synthesis and preserve authored posts."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.engine.flow import AnswerFlow
from agent.engine.test_agent_plan_flow import _context
from agent.discord_actions import test_current_channel_posts as posts
from elbow_helper.agent.discord_actions.messages import prepare_post


def players():
    return {"source": "clash_api", "kind": "player", "flags": {"status": "complete"}, "items": [
        {"tag": "#P0", "status": "ok", "data": {"name": "North"}},
        {"tag": "#P2", "status": "ok", "data": {"name": "South"}},
    ]}


def query(rows):
    return {"rows": rows, "row_count": len(rows), "truncated": False, "flags": {"status": "complete"}}


class StrictFlowTests(unittest.IsolatedAsyncioTestCase):

    async def test_names_and_reasons_are_preserved_with_multiple_sources(self):
        for evidence in ({"read": players(), "other": query([{"name": "North", "reason": "Away"}])}, {}):
            context = _context()
            context.state.request_text = "Just the names of people who missed attacks, and why"
            context.state.grounding_evidence = deepcopy(evidence)
            rounder = SimpleNamespace(advance=AsyncMock())
            flow = AnswerFlow(context=context, session=None, registry={}, budget=None,
                              rounder=rounder, runner=None, request_id=1)
            draft = "North: away. South: no reason recorded."
            self.assertEqual(await flow._grounded_reply(draft), draft)
            rounder.advance.assert_not_awaited()


class StrictPostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await posts.CurrentChannelPostTests.asyncSetUp(self)

    async def test_current_channel_post_preserves_authored_text(self):
        self.context.state.request_text = "names only"
        self.context.state.grounding_evidence = {"read": players()}
        result = await prepare_post(self.context, {"channel_id": 2, "text": "A joke instead of names"})
        self.assertTrue(result["delivered_as_reply"])
        self.context.send_reply.assert_awaited_with(self.context, "A joke instead of names")
