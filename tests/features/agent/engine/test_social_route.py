"""Short social replies combine reactions with text or a GIF."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord

from elbow_helper.features.agent.engine.routing import social_candidate, SOCIAL_CAPABILITIES
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from features.agent.engine.test_agent_service import _AgentSession, _AgentModel, _context
from features.agent.discord_actions.test_gif_delivery import candidate_client
from tests.features.agent_plan_helpers import plan_call


class SocialRouteTests(unittest.IsolatedAsyncioTestCase):
    def test_media_and_social_one_liners_select_the_small_route(self):
        for text in ("MY MAN", "<@999> thanks!", "lol", "roast me"):
            self.assertTrue(social_candidate(text, SimpleNamespace(content=text)))
        for media in (
            {"stickers": [object()]},
            {"attachments": [SimpleNamespace(filename="synthetic.png", content_type="image/png")]},
            {"content": "https://tenor.com/view/synthetic"},
        ):
            self.assertTrue(social_candidate("Respond to this media", SimpleNamespace(**media)))
        for text in ("how many members", "thanks, but correct those numbers", "delete that message",
                     "I need help with recruitment", "x" * 301):
            self.assertFalse(social_candidate(text, SimpleNamespace(content=text)))

    async def test_reaction_with_text_or_gif_needs_one_low_effort_round(self):
        for mode in ("reaction", "gif", "both"):
            with self.subTest(mode=mode):
                context = _context()
                context.source_message.id = 300
                context.source_message.add_reaction = AsyncMock()
                client = candidate_client(AsyncMock(return_value="https://giphy.com/gifs/synthetic"))
                knowledge = Mock()
                context = replace(context, bot=SimpleNamespace(gif_client=client), knowledge_store=knowledge)
                calls = []
                if mode != "gif":
                    calls.append(AgentToolCall("react", "react_to_request", json.dumps({"emoji": "👍"})))
                if mode != "reaction":
                    calls.append(AgentToolCall("gif", "find_gif", json.dumps({"query": "synthetic salute"})))
                text = "Synthetic acknowledgement." if mode == "reaction" else ""
                session = _AgentSession([AgentStep(text, (plan_call(*calls),), AgentUsage())])
                model = _AgentModel(session)
                answer = await AgentService(model).answer(question="MY MAN", local_context="", context=context)
                self.assertEqual(answer, text)
                self.assertEqual(session.efforts, [AgentReasoningEffort.LOW])
                self.assertEqual(context.source_message.add_reaction.await_count, int(mode != "gif"))
                self.assertEqual(bool(context.state.gif_url), mode != "reaction")
                offered = model.request["tools"][0].parameters["properties"]["steps"]["items"]["properties"]["capability"]["enum"]
                self.assertEqual(set(offered), SOCIAL_CAPABILITIES)
                knowledge.load.assert_not_called()
                self.assertIn("Accompany reactions with one short line or a GIF", model.request["system_prompt"])

    async def test_empty_reaction_plan_still_gets_a_reply_without_configured_gifs(self):
        context = _context()
        context.source_message.id = 300
        context.source_message.add_reaction = AsyncMock()
        step = AgentStep("", (plan_call(AgentToolCall(
            "react", "react_to_request", json.dumps({"emoji": "👍"}),
        )),), AgentUsage())
        session = _AgentSession([step, AgentStep("Synthetic acknowledgement.", (), AgentUsage())])
        model = _AgentModel(session)
        self.assertEqual(await AgentService(model).answer(
            question="thanks", local_context="", context=context,
        ), "Synthetic acknowledgement.")
        self.assertEqual(len(session.calls), 2)
        context.source_message.add_reaction.assert_awaited_once()
        offered = model.request["tools"][0].parameters["properties"]["steps"]["items"]["properties"]["capability"]["enum"]
        self.assertEqual(offered, ["react_to_request"])

    async def test_media_request_can_expand_for_serious_work_without_reacting(self):
        context = _context()
        context.source_message.stickers = [SimpleNamespace(name="Synthetic chart")]
        session = _AgentSession([
            AgentStep("", (AgentToolCall("expand", "load_research_context", "{}"),), AgentUsage()),
            AgentStep("Synthetic factual answer.", (), AgentUsage()),
        ])
        model = _AgentModel(session)
        answer = await AgentService(model).answer(
            question="Explain this chart", local_context="", context=context,
        )
        self.assertEqual(answer, "Synthetic factual answer.")
        self.assertEqual(session.efforts, [AgentReasoningEffort.LOW, AgentReasoningEffort.HIGH])
        self.assertEqual(len(session.tool_replacements), 1)
        self.assertIn("read_clash", str(session.tool_replacements[0]))
        self.assertEqual(context.state.reactions.emojis, [])
        self.assertIsNone(context.state.gif_url)

    async def test_failed_reaction_returns_to_the_model_for_a_text_reply(self):
        context = _context()
        context.source_message.id = 300
        context.source_message.add_reaction = AsyncMock(side_effect=discord.Forbidden(
            SimpleNamespace(status=403, reason="Synthetic"), "Synthetic rejection"))
        session = _AgentSession([
            AgentStep("", (plan_call(AgentToolCall(
                "react", "react_to_request", json.dumps({"emoji": "\U0001f44d"}),
            )),), AgentUsage()),
            AgentStep("Synthetic acknowledgement.", (), AgentUsage()),
        ])
        answer = await AgentService(_AgentModel(session)).answer(
            question="thanks", local_context="", context=context,
        )
        self.assertEqual(answer, "Synthetic acknowledgement.")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(context.state.reactions.emojis, [])

    async def test_social_route_can_answer_a_correction_in_words_without_tools(self):
        context = _context()
        context.source_message.stickers = [SimpleNamespace(name="Synthetic chart")]
        context.source_message.add_reaction = AsyncMock()
        session = _AgentSession([AgentStep("Synthetic correction.", (), AgentUsage())])
        answer = await AgentService(_AgentModel(session)).answer(
            question="Correct your numbers", local_context="", context=context,
        )
        self.assertEqual(answer, "Synthetic correction.")
        self.assertEqual(session.efforts, [AgentReasoningEffort.LOW])
        context.source_message.add_reaction.assert_not_awaited()
