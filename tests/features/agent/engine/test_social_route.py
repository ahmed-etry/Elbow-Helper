"""Short social replies combine reactions with text or a GIF."""

from dataclasses import replace
from features.agent.engine.helpers import LegacyCatalogueMixin

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from elbow_helper.features.agent.engine.routing import (
    judge_route, ROUTE_INSTRUCTION, ROUTE_OUTPUT_TOKENS,
)
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.conversation.state import ConversationTurn
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage, TextGenerationError
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from features.agent.engine.test_agent_service import _AgentSession, _AgentModel, _context
from features.agent.discord_actions.test_gif_delivery import candidate_client
from tests.features.agent_plan_helpers import plan_call
from features.agent.engine.helpers import social_step
from features.agent.engine.test_banter_gifs import gif_context


class SocialRouteTests(LegacyCatalogueMixin, unittest.IsolatedAsyncioTestCase):
    async def test_combo_failure_keeps_social_text_and_research_cannot_use_an_unoffered_combo(self):
        for route in ("social", "research"):
            for all_failed in (False, True):
                with self.subTest(route=route, all_failed=all_failed):
                    context = _context()
                    context.state.channel_bot_combos = ()
                    context.source_message.id = 300
                    failure = discord.HTTPException(SimpleNamespace(status=500, reason="synthetic"), "synthetic")
                    context.source_message.add_reaction = AsyncMock(
                        side_effect=[failure, failure, failure] if all_failed else [None, failure, None],
                    )
                    call = plan_call(AgentToolCall("react", "react_to_request", json.dumps({
                        "emoji": ["\U0001f914", "\U0001f4a1", "\U0001f680"],
                    })))
                    plan = json.loads(call.arguments)
                    plan["reply"] = "Synthetic punchline."
                    call = replace(call, arguments=json.dumps(plan))
                    session = _AgentSession([social_step("Synthetic punchline.", form="text_reaction_combo",
                                                         reactions=["\U0001f914", "\U0001f4a1", "\U0001f680"])
                                             if route == "social" else AgentStep("", (call,), AgentUsage())])
                    answer = await AgentService(_AgentModel(session, route=route)).answer(
                        question="The imaginary cardboard throne collapsed.", local_context="", context=context,
                    )
                    self.assertEqual(answer, "Synthetic punchline.")
                    self.assertEqual(len(session.calls), 1)
                    self.assertEqual(context.source_message.add_reaction.await_count, 3 if route == "social" else 0)
                    self.assertEqual(context.state.reactions.emojis,
                                     [] if all_failed or route == "research" else ["\U0001f914", "\U0001f680"])

    async def test_optional_gif_failure_keeps_planned_text_without_another_round(self):
        context = _context()
        context.source_message.id = 300
        context = replace(gif_context(context), bot=SimpleNamespace(gif_client=candidate_client(AsyncMock(return_value=None))))
        call = plan_call(AgentToolCall("gif", "find_gif", '{"query":"synthetic acknowledgement"}'))
        plan = json.loads(call.arguments)
        plan["reply"] = "Synthetic acknowledgement."
        session = _AgentSession([
            social_step("Synthetic acknowledgement.", meme_id="synthetic acknowledgement"),
        ])
        answer = await AgentService(_AgentModel(session, route="social")).answer(question="you up?", local_context="", context=context)
        self.assertEqual(answer, "Synthetic acknowledgement.")
        self.assertEqual(len(session.calls), 1)
        self.assertIsNone(context.state.gif_url)

    async def test_model_judgment_routes_synthetic_chat_without_a_word_list(self):
        for question, decision in (
            ("Evening, village gremlins", "social"),
            ("spectacularly toasted", "social"),
            ("glorptastic", "social"),
            ("Who runs this imaginary goblin kingdom?", "social"),
            ("Who deserves the imaginary nap crown around here?", "social"),
            ("Is the pretend cardboard monarch a certified moon goblin? Agree briefly.", "social"),
            ("The pretend decree still stands. Keep the bit going.", "social"),
            ("heh", "social"),
            ("That punchline needs a funeral.", "social"),
            ("yep " * 120, "social"),
            ("Which player landed the most triples yesterday, mate?", "research"),
            ("Did Nyx leave Moon Clan already haha?", "research"),
            ("heh, remove that post", "research"),
            ("Wait, those totals are wrong", "research"),
            ("Your answer ignored my requested format.", "research"),
            ("Who has the highest war triple rate this week?", "research"),
        ):
            with self.subTest(question=question):
                session = _AgentSession([social_step("Synthetic answer.") if decision == "social"
                                         else AgentStep("Synthetic answer.", (), AgentUsage())])
                model = _AgentModel(session, route=decision)
                await AgentService(model).answer(
                    question=question, local_context="Synthetic nearby exchange.", context=_context(),
                    conversation_history="Synthetic earlier bot banter.",
                )
                offered = model.request["tools"]
                self.assertEqual(len(offered), 2 if decision == "social" else 1)
                self.assertEqual(session.efforts, [AgentReasoningEffort.LOW if decision == "social"
                                                   else AgentReasoningEffort.HIGH])
                self.assertEqual(model.routing_request["tools"], ())
                self.assertEqual(model.routing_request["system_prompt"], ROUTE_INSTRUCTION)
                self.assertEqual(json.loads(model.routing_request["prompt"])["request"], question)
                self.assertIn("Synthetic earlier bot banter.", model.routing_request["prompt"])
                self.assertEqual(model.routing_session.calls, [{
                    "allow_tools": False, "reasoning_effort": AgentReasoningEffort.LOW,
                    "max_output_tokens": ROUTE_OUTPUT_TOKENS,
                }])

    async def test_invalid_truncated_or_unavailable_judgment_uses_research(self):
        for step in (
            AgentStep("unclear", (), AgentUsage()),
            AgentStep("social because it is short", (), AgentUsage()),
            AgentStep("social", (), AgentUsage(), output_limit_reached=True),
            AgentStep("social", (AgentToolCall("x", "unexpected", "{}"),), AgentUsage()),
            TextGenerationError("Synthetic provider failure"),
            TimeoutError(),
        ):
            session = Mock()
            session.advance = AsyncMock(side_effect=step if isinstance(step, Exception) else None,
                                        return_value=step)
            model = Mock(create_agent_session=Mock(return_value=session))
            route = await judge_route(model, question="synthetic", local_context="",
                                      conversation_history="", context=_context())
            self.assertFalse(route.social)

    async def test_gif_availability_tracks_configuration_on_both_routes(self):
        for route in ("social", "research"):
            for configured in (False, True):
                context = replace(_context(), bot=SimpleNamespace(
                    gif_client=SimpleNamespace(configured=configured),
                ))
                session = _AgentSession([social_step("Synthetic acknowledgement.") if route == "social"
                                         else AgentStep("Synthetic acknowledgement.", (), AgentUsage())])
                model = _AgentModel(session, route=route)
                await AgentService(model).answer(
                    question="Synthetic conversational moment", local_context="", context=context,
                )
                definition = model.request["tools"][0]
                if route == "social":
                    forms = definition.parameters["properties"]["reply_form"]["enum"]
                    self.assertNotIn("gif", forms)
                    self.assertIn("meme_shortlist", definition.parameters["required"])
                    self.assertIn("text_reactions", forms)
                    self.assertEqual(definition.name, "submit_social_reply")
                else:
                    offered = definition.parameters["properties"]["steps"]["items"]["properties"]["capability"]["enum"]
                    self.assertNotIn("find_gif", offered)
                self.assertEqual({tool.name for tool in model.request["tools"][1:]},
                                 {"load_research_context"} if route == "social" else set())
                if configured and route == "social":
                    self.assertNotIn("system paces GIF selection", model.request["system_prompt"])

    async def test_routing_tokens_are_included_in_completion_accounting(self):
        session = _AgentSession([replace(social_step(), usage=AgentUsage(400, 20, 0, 400))])
        model = _AgentModel(session, route="social")
        with patch.object(AgentService, "_log_completion") as logged:
            await AgentService(model).answer(question="Synthetic greeting", local_context="", context=_context())
        usage = logged.call_args.kwargs["usage"]
        self.assertEqual((usage.attempted_rounds, usage.reported_rounds), (2, 2))
        self.assertEqual((usage.prompt_tokens, usage.completion_tokens), (500, 21))

    async def test_reply_forms_need_one_low_effort_round(self):
        for form in ("text", "text_reactions"):
            with self.subTest(form=form):
                context = _context()
                context.source_message.id = 300
                context.source_message.add_reaction = AsyncMock()
                client = candidate_client(AsyncMock(return_value="https://giphy.com/gifs/synthetic"))
                knowledge = Mock()
                context = replace(context, bot=SimpleNamespace(gif_client=client, agent_model=client.agent_model), knowledge_store=knowledge)
                text = "" if form == "gif" else "Synthetic acknowledgement."
                session = _AgentSession([social_step(text, form=form,
                    meme_id=None,
                    reactions=["\U0001f44d"] if form == "text_reactions" else [])])
                model = _AgentModel(session, route="social")
                answer = await AgentService(model).answer(question="MY MAN", local_context="", context=context)
                self.assertEqual(answer, text)
                self.assertEqual(session.efforts, [AgentReasoningEffort.LOW])
                self.assertEqual(context.source_message.add_reaction.await_count, int(form == "text_reactions"))
                self.assertEqual(bool(context.state.gif_url), form.startswith("gif"))
                knowledge.load.assert_not_called()
                self.assertIn("Never reply with reactions alone", model.request["system_prompt"])

    async def test_reaction_only_plan_is_retried_before_any_reaction(self):
        context = _context()
        context.source_message.id = 300
        context.source_message.add_reaction = AsyncMock()
        step = AgentStep("", (plan_call(AgentToolCall(
            "react", "react_to_request", json.dumps({"emoji": "\U0001f44d"}),
        )),), AgentUsage())
        session = _AgentSession([step, social_step("Synthetic acknowledgement.")])
        model = _AgentModel(session, route="social")
        self.assertEqual(await AgentService(model).answer(
            question="thanks", local_context="", context=context,
        ), "Synthetic acknowledgement.")
        self.assertEqual(len(session.calls), 2)
        context.source_message.add_reaction.assert_not_awaited()

    async def test_social_route_can_expand_for_serious_work_without_reacting(self):
        context = _context()
        context.source_message.stickers = [SimpleNamespace(name="Synthetic chart")]
        session = _AgentSession([
            AgentStep("", (AgentToolCall("expand", "load_research_context", "{}"),), AgentUsage()),
            AgentStep("Synthetic factual answer.", (), AgentUsage()),
        ])
        model = _AgentModel(session, route="social")
        answer = await AgentService(model).answer(
            question="Respond to the image, sticker or GIF in this message.", local_context="", context=context,
        )
        self.assertEqual(answer, "Synthetic factual answer.")
        self.assertEqual(session.efforts, [AgentReasoningEffort.LOW, AgentReasoningEffort.HIGH])
        self.assertEqual(len(session.tool_replacements), 1)
        self.assertIn("read_clash", str(session.tool_replacements[0]))
        self.assertEqual(context.state.reactions.emojis, [])
        self.assertIsNone(context.state.gif_url)

    async def test_social_history_is_public_and_expansion_restores_all_authorized_turns(self):
        context = _context()
        original = tuple(ConversationTurn(f"synthetic turn {i}", frozenset()) for i in range(6))
        context.state.authorized_history = original
        session = _AgentSession([
            AgentStep("", (AgentToolCall("expand", "load_research_context", "{}"),), AgentUsage()),
            AgentStep("Synthetic factual answer.", (), AgentUsage()),
        ])
        model = _AgentModel(session, route="social")
        with patch("elbow_helper.features.agent.engine.service.can_disclose_provenance",
                   AsyncMock(side_effect=[True, True, True, False])) as public:
            await AgentService(model).answer(question="Synthetic follow-up", local_context="", context=context)
        self.assertEqual(public.await_count, 4)
        for i in (0, 1, 5):
            self.assertNotIn(f"synthetic turn {i}", model.request["prompt"])
        for i in (2, 3, 4):
            self.assertIn(f"synthetic turn {i}", model.request["prompt"])
        for i in range(6):
            self.assertIn(f"synthetic turn {i}", session.calls[1][0][0].content)
        self.assertIs(context.state.authorized_history, original)

    async def test_failed_reaction_returns_to_the_model_for_a_text_reply(self):
        context = _context()
        context.source_message.id = 300
        context.source_message.add_reaction = AsyncMock(side_effect=discord.Forbidden(
            SimpleNamespace(status=403, reason="Synthetic"), "Synthetic rejection"))
        session = _AgentSession([social_step("Synthetic acknowledgement.", form="text_reactions",
                                             reactions=["\U0001f44d"])])
        answer = await AgentService(_AgentModel(session, route="social")).answer(
            question="thanks", local_context="", context=context,
        )
        self.assertEqual(answer, "Synthetic acknowledgement.")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(context.state.reactions.emojis, [])

    async def test_correction_with_media_starts_full_route(self):
        context = _context()
        context.source_message.stickers = [SimpleNamespace(name="Synthetic chart")]
        context.source_message.add_reaction = AsyncMock()
        session = _AgentSession([AgentStep("Synthetic correction.", (), AgentUsage())])
        answer = await AgentService(_AgentModel(session)).answer(
            question="Correct your numbers", local_context="", context=context,
        )
        self.assertEqual(answer, "Synthetic correction.")
        self.assertEqual(session.efforts, [AgentReasoningEffort.HIGH])
        context.source_message.add_reaction.assert_not_awaited()
