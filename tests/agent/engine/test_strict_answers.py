"""Formatting requests still use answer synthesis and preserve authored posts."""

import unittest

from tests.agent.discord_actions import test_current_channel_posts as posts
from elbow_helper.agent.discord_actions.messages import prepare_post


def players():
    return {"source": "clash_api", "kind": "player", "flags": {"status": "complete"}, "items": [
        {"tag": "#P0", "status": "ok", "data": {"name": "North"}},
        {"tag": "#P2", "status": "ok", "data": {"name": "South"}},
    ]}


class StrictPostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await posts.CurrentChannelPostTests.asyncSetUp(self)

    async def test_current_channel_post_preserves_authored_text(self):
        self.context.state.request_text = "names only"
        self.context.state.grounding_evidence = {"read": players()}
        result = await prepare_post(self.context, {"channel_id": 2, "text": "A joke instead of names"})
        self.assertTrue(result["delivered_as_reply"])
        self.context.send_reply.assert_awaited_with(self.context, "A joke instead of names")
