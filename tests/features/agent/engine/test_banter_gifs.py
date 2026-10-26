"""Offline social-output checks using the real plan, GIF client and delivery path."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock


from elbow_helper.features.agent.conversation.state import ConversationRecord, ConversationTurn
from elbow_helper.features.agent.discord_actions.gifs import find_gif
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage
from elbow_helper.infrastructure.gifs import GiphyClient
from features.agent.engine.test_agent_service import _AgentModel, _AgentSession, _context
from tests.features.agent_plan_helpers import plan_call
from features.agent.engine.helpers import social_step


def delivered_turn(answer, *, generated=None):
    record = ConversationRecord(
        request_message_id=200, member_id=42, created_at="2026-10-01T00:00:00+00:00",
        question="Synthetic exchange", generated_answer=generated or answer,
        delivered_answer=answer, local_context="", evidence=(), report_ids=(),
        reply_ids=(201,) if answer else (), delivery_complete=bool(answer),
    )
    return ConversationTurn("Synthetic retained turn", frozenset({100}), record=record)


def due_history():
    return tuple(delivered_turn("Synthetic earlier reply.") for _ in range(5))


def gif_context(context=None, *, recent=()):
    """Supply an authorized channel snapshot independently of retained turns."""
    context = _context() if context is None else context
    context.state.channel_bot_gifs = tuple(recent)
    return context


def gif_plan(*, reply=None):
    call = plan_call(AgentToolCall("gif", "find_gif", '{"query":"synthetic disbelief"}'))
    if reply is not None:
        plan = json.loads(call.arguments)
        plan["reply"] = reply
        call = replace(call, arguments=json.dumps(plan))
    return call


class ScriptedGifModel(_AgentModel):
    """Keep routing, the reply plan and candidate vision in distinct sessions."""

    def __init__(self, session, *, route):
        super().__init__(session, route=route)
        self.vision = _AgentSession([AgentStep("2", (), AgentUsage())])
        self.vision_request = None
        self.complete = AsyncMock(return_value="2")

    def create_agent_session(self, **kwargs):
        if kwargs.get("image_failure_fallback") is False:
            self.vision_request = kwargs
            return self.vision
        return super().create_agent_session(**kwargs)


class BanterGifTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_gif_does_not_block_optional_selection(self):
        from features.agent.discord_actions.test_gif_delivery import candidate_client
        client = candidate_client(AsyncMock(return_value="https://giphy.com/gifs/new"))
        context = replace(gif_context(recent=(True,)), bot=SimpleNamespace(
            gif_client=client, agent_model=client.agent_model), history=())
        result = await find_gif(context, {"query": "synthetic disbelief"})
        self.assertEqual(result, {"url": "https://giphy.com/gifs/new"})
        client.search_candidates.assert_awaited_once()

    async def test_unavailable_provider_keeps_the_planned_fallback(self):
        for status, data in ((200, []), (503, [])):
            with self.subTest(status=status):
                client = GiphyClient("synthetic-key")
                response = SimpleNamespace(status=status, json=AsyncMock(return_value={"data": data}))
                request = MagicMock()
                request.__aenter__ = AsyncMock(return_value=response)
                request.__aexit__ = AsyncMock(return_value=None)
                client._session = SimpleNamespace(closed=False, get=MagicMock(return_value=request),
                                                 close=AsyncMock())
                session = _AgentSession([
                    social_step("Synthetic fallback.", meme_id="synthetic disbelief", gif_with_text=True),
                ])
                model = ScriptedGifModel(session, route="social")
                context = replace(gif_context(), bot=SimpleNamespace(gif_client=client, agent_model=model))
                try:
                    answer = await AgentService(model).answer(
                        question="The imaginary tower fell over.", local_context="", context=context,
                    )
                    self.assertEqual(answer, "Synthetic fallback.")
                    self.assertIsNone(context.state.gif_url)
                    self.assertEqual(len(session.calls), 1)
                    self.assertIsNone(model.vision_request)
                finally:
                    await client.close()
