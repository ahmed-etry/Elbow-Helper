"""Synthetic GIF replies retain one link and use separate tracked Discord messages."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.discord_actions.gifs import find_gif
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.models import AgentDelivery
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage
from features.agent.engine.test_agent_service import _AgentModel, _AgentSession, _context
from tests.features.agent_plan_helpers import plan_call


class GifDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_later_search_replaces_the_retained_gif(self):
        context = _context()
        client = SimpleNamespace(search=AsyncMock(side_effect=["first", "second"]))
        context = replace(context, bot=SimpleNamespace(gif_client=client))
        await find_gif(context, {"query": "synthetic first"})
        await find_gif(context, {"query": "synthetic second"})
        self.assertEqual(context.state.gif_url, "second")

    async def test_delivery_is_link_only_after_text_or_on_its_own(self):
        for response in ("Synthetic answer", ""):
            context = _context()
            context.state.gif_url = "https://giphy.com/gifs/synthetic"
            sent = SimpleNamespace(id=800)
            channel = context.source_message.channel
            channel.send = AsyncMock(return_value=sent)
            message = SimpleNamespace(id=300, channel=channel, archive_reply=False,
                                      reply=AsyncMock(return_value=sent))
            surface = AgentDeliveryMixin()
            surface._send_response = AsyncMock()
            delivery = AgentDelivery()
            if response:
                delivery.record(700, response)
            conversation = MagicMock()
            surface._conversations = MagicMock()
            with (
                patch("elbow_helper.features.agent.delivery.require_evidence_access", AsyncMock()),
                patch("elbow_helper.features.agent.delivery.can_show",
                      AsyncMock(return_value=True)),
            ):
                await surface.send_response(message, response, None, conversation=conversation,
                                            context=context, delivery=delivery)
            call = (channel.send if response else message.reply).await_args
            self.assertEqual(call.args, (context.state.gif_url,))
            self.assertFalse(call.kwargs["allowed_mentions"].users)
            self.assertFalse(call.kwargs["allowed_mentions"].replied_user)
            self.assertEqual(delivery.text_parts[-1], context.state.gif_url)
            self.assertTrue(delivery.complete)
            surface._conversations.register_reply.assert_called_once_with(conversation, 800)

    async def test_gif_and_reaction_plan_finishes_in_one_model_round(self):
        context = _context()
        context.source_message.id = 300
        context.source_message.add_reaction = AsyncMock()
        client = SimpleNamespace(configured=True, search=AsyncMock(return_value="synthetic-link"))
        context = replace(context, bot=SimpleNamespace(gif_client=client))
        registry = build_agent_tools(client)
        step = AgentStep("", (plan_call(
            AgentToolCall("reaction", "react_to_request", json.dumps({"emoji": "\U0001f44d"})),
            AgentToolCall("gif", "find_gif", json.dumps({"query": "synthetic"})),
        ),), AgentUsage())
        session = _AgentSession([step])
        service = AgentService(_AgentModel(session))
        with patch("elbow_helper.features.agent.engine.service.build_agent_tools",
                   return_value=registry):
            answer = await service.answer(question="Synthetic request", local_context="",
                                          context=context)
        self.assertEqual(answer, "")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(context.state.gif_url, "synthetic-link")

    async def test_plan_searches_replace_in_step_order(self):
        context = _context()
        client = SimpleNamespace(configured=True,
                                 search=AsyncMock(side_effect=["first", "second"]))
        context = replace(context, bot=SimpleNamespace(gif_client=client))
        session = _AgentSession([
            AgentStep("", (plan_call(
                AgentToolCall("first", "find_gif", json.dumps({"query": "synthetic first"})),
                AgentToolCall("second", "find_gif", json.dumps({"query": "synthetic second"})),
            ),), AgentUsage()),
            AgentStep("Synthetic answer", (), AgentUsage()),
        ])
        await AgentService(_AgentModel(session)).answer(question="Synthetic request",
                                                       local_context="", context=context)
        self.assertEqual(context.state.gif_url, "second")
        self.assertEqual([call.args[0] for call in client.search.await_args_list],
                         ["synthetic first", "synthetic second"])

    async def test_previous_disclosure_applies_to_the_gif_part(self):
        context = _context()
        context.state.gif_url = "https://giphy.com/gifs/synthetic"
        channel = context.source_message.channel
        channel.send = AsyncMock(return_value=SimpleNamespace(id=800))
        message = SimpleNamespace(id=300, channel=channel, archive_reply=False)
        surface = AgentDeliveryMixin()
        surface._send_response = AsyncMock()
        surface._conversations = MagicMock()
        conversation = MagicMock()
        conversation.can_reuse_answer_disclosure.return_value = True
        delivery = AgentDelivery()
        delivery.record(700, "Synthetic answer")
        with (
            patch("elbow_helper.features.agent.delivery.require_evidence_access", AsyncMock()),
            patch("elbow_helper.features.agent.delivery.can_show", AsyncMock(return_value=False)),
        ):
            await surface.send_response(message, "Synthetic answer", None,
                                        conversation=conversation, context=context,
                                        delivery=delivery)
        channel.send.assert_awaited_once()
        self.assertTrue(delivery.complete)

    async def test_interruption_after_text_keeps_delivery_incomplete(self):
        from elbow_helper.features.agent.access import AgentAccessLost
        context = _context()
        context.state.gif_url = "https://giphy.com/gifs/synthetic"
        message = SimpleNamespace(id=300, channel=context.source_message.channel)
        delivery = AgentDelivery()

        async def text_reply(*args, **kwargs):
            delivery.record(700, "Synthetic answer")
            delivery.complete = True

        surface = AgentDeliveryMixin()
        surface._send_response = AsyncMock(side_effect=text_reply)
        with patch("elbow_helper.features.agent.delivery.require_evidence_access",
                   AsyncMock(side_effect=AgentAccessLost("Synthetic loss"))):
            with self.assertRaises(AgentAccessLost):
                await surface.send_response(message, "Synthetic answer", None,
                                            context=context, delivery=delivery)
        self.assertEqual(delivery.message_ids, [700])
        self.assertFalse(delivery.complete)
