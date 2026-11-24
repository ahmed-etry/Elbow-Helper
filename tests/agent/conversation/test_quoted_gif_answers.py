"""A research answer quoting a GIF link is delivered as text; only the GIF tool posts a GIF."""

from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.conversation.delivery import AgentDeliveryMixin
from elbow_helper.agent.engine.models import AgentDelivery
from tests.agent.request_fixtures import flow_context as _context


GIF = "https://tenor.com/view/synthetic-reaction-123"
ANSWER = ("The last three messages in #synthetic-leads:\n"
          "1. Synthetic lead A: War lineup is posted.\n"
          "2. Synthetic lead B: I'll check the bases tonight.\n"
          "3. Synthetic lead C: " + GIF)


class QuotedGifAnswerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = _context()
        self.message = self.context.source_message
        self.message.mentions, self.message.archive_reply = [], False
        self.message.author = self.context.member
        self.message.reply = AsyncMock(return_value=SimpleNamespace(id=701))
        self.message.channel.send = AsyncMock(return_value=SimpleNamespace(id=702))
        self.surface = AgentDeliveryMixin()
        self.surface.bot = SimpleNamespace(user=self.context.guild.me)
        self.surface._send_gif = AsyncMock(wraps=self.surface._send_gif)
        guards = ExitStack()
        for name in ("require_evidence_access", "assert_answer_scope"):
            guards.enter_context(patch("elbow_helper.agent.conversation.delivery." + name, AsyncMock()))
        self.addCleanup(guards.close)

    async def test_research_reply_without_text_never_sends_a_lone_gif(self):
        delivery = AgentDelivery()
        await self.surface.send_response(self.message, "", None,
                                         context=self.context, delivery=delivery)
        self.surface._send_gif.assert_not_awaited()
        sent = self.message.reply.await_args.args[0]
        self.assertTrue(sent.strip())
        self.assertNotIn("tenor.com", sent)
