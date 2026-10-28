"""A research answer quoting a GIF link is delivered as text; only the GIF tool posts a GIF."""

from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.delivery import AgentDeliveryMixin
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.families.core import family_tool
from elbow_helper.agent.models import AgentDelivery
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan


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
            guards.enter_context(patch("elbow_helper.agent.delivery." + name, AsyncMock()))
        self.addCleanup(guards.close)

    async def research_answer(self):
        rows = [{"message_id": 9001 + index, "author": author, "content": content}
                for index, (author, content) in enumerate((
                    ("Synthetic lead A", "War lineup is posted."),
                    ("Synthetic lead B", "I'll check the bases tonight."),
                    ("Synthetic lead C", GIF)))]
        source = AsyncMock(return_value={"rows": rows, "summary": {
            "requester_id": self.context.member.id, "mode": "channel_window", "channel_id": 92}})
        tool = family_tool("read_conversation_evidence", "Read discussion.", {}, source)
        plan = {**_plan([{"id": "window", "capability": "read_conversation_evidence",
                          "arguments": {}, "depends_on": []}]), "finish_after_reads": True}
        session = _Session([_model_step(plan), AgentStep(ANSWER, (), AgentUsage(1000, 870))], [])
        with patch("elbow_helper.agent.engine.service.build_agent_tools",
                   return_value={"read_conversation_evidence": tool}):
            answer = await AgentService(_Model(session)).answer(
                question="Show the last three messages in #synthetic-leads",
                local_context="", context=self.context)
        source.assert_awaited_once()
        return answer

    async def test_research_answer_quoting_a_gif_link_sends_the_full_text_once(self):
        answer = await self.research_answer()
        self.assertIn(GIF, answer)
        self.assertIsNone(self.context.state.gif_url)
        delivery = AgentDelivery()
        await self.surface.send_response(self.message, answer, None,
                                         context=self.context, delivery=delivery)
        self.message.reply.assert_awaited_once()
        self.assertEqual(self.message.reply.await_args.args, (answer,))
        self.message.channel.send.assert_not_awaited()
        self.surface._send_gif.assert_not_awaited()
        self.assertEqual(delivery.text_parts, [answer])
        self.assertTrue(delivery.complete)

    async def test_research_reply_without_text_never_sends_a_lone_gif(self):
        delivery = AgentDelivery()
        await self.surface.send_response(self.message, "", None,
                                         context=self.context, delivery=delivery)
        self.surface._send_gif.assert_not_awaited()
        sent = self.message.reply.await_args.args[0]
        self.assertTrue(sent.strip())
        self.assertNotIn("tenor.com", sent)

    async def test_banter_gif_from_the_gif_tool_still_posts_on_its_own(self):
        self.context.state.gif_url = GIF
        delivery = AgentDelivery()
        await self.surface.send_response(self.message, "Synthetic banter.", None,
                                         context=self.context, delivery=delivery)
        self.message.reply.assert_awaited_once()
        self.assertEqual(self.message.reply.await_args.args, (GIF,))
        self.assertEqual(delivery.text_parts, [GIF])
        self.assertEqual(self.context.state.gif_fallback_text, "Synthetic banter.")
