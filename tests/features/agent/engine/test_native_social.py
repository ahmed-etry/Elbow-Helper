"""Exercise native social outputs through provider serialization and checked execution."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from features.agent.engine.helpers import LegacyCatalogueMixin
from elbow_helper.features.agent.wording import AGENT_ANSWER_UNFINISHED
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.discord_actions.gif_library import ITEM_BY_ID
from elbow_helper.features.agent.engine.routing import ROUTE_INSTRUCTION
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.models import AgentDelivery
from elbow_helper.features.agent.plan.planning import request_plan
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage, DeepSeekTextClient, TextGenerationError
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from elbow_helper.features.agent.wording import FAILURE_MESSAGE
from features.agent.engine.test_agent_service import _AgentModel, _AgentSession, _context
from features.agent.engine.helpers import social_step
from features.agent.discord_actions.test_gif_delivery import candidate_client
from features.agent.engine.test_banter_gifs import gif_context


class NativeSocialTests(LegacyCatalogueMixin, unittest.IsolatedAsyncioTestCase):
    async def test_native_meme_reaches_provider_payload_and_delivery_without_search(self):
        requests = []

        async def provider(**options):
            requests.append(deepcopy(options))
            system = options["messages"][0]["content"]
            calls = []
            if system == ROUTE_INSTRUCTION:
                content = "social"
            elif system.startswith("Choose the GIF"):
                self.assertEqual(options["max_tokens"], 8)
                self.assertEqual(options["extra_body"], {"thinking": {"type": "disabled"}})
                self.assertNotIn("tools", options)
                self.assertIsInstance(options["messages"][1]["content"], str)
                self.assertIn("Synthetic astonished cat 1", options["messages"][1]["content"])
                self.assertIn("A cat looks surprised", options["messages"][1]["content"])
                content = "2"
            else:
                names = {tool["function"]["name"] for tool in options["tools"]}
                self.assertIn("submit_social_reply", names)
                self.assertNotIn("find_gif", names)
                self.assertNotEqual(options.get("tool_choice"), "none")
                function = next(tool["function"] for tool in options["tools"]
                                if tool["function"]["name"] == "submit_social_reply")
                self.assertIn("reply_form", function["parameters"]["required"])
                calls = [{"id": "native-gif", "type": "function", "function": {
                    "name": "submit_social_reply", "arguments": social_step("Synthetic fallback.", meme_id="ts_lowkey_tuff").tool_calls[0].arguments,
                }}]
                content = ""
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", content=content,
                                                   tool_calls=calls), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10,
                                      prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=100),
                id="synthetic-provider-request", model="synthetic-provider",
            )

        create = AsyncMock(side_effect=provider)
        transport = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                                    close=AsyncMock())
        transport.with_options = lambda **_: transport
        model = DeepSeekTextClient(None)
        model._client = transport
        from elbow_helper.infrastructure.gifs import GiphyClient
        from unittest.mock import MagicMock

        client = GiphyClient("synthetic-key")
        response = SimpleNamespace(status=200, json=AsyncMock(return_value={"data": [
            {"url": f"https://giphy.com/gifs/synthetic-{index}",
             "title": f"Synthetic astonished cat {index}", "alt_text": "A cat looks surprised",
             "images": {
                "original_still": {"url": f"https://media.giphy.com/synthetic-{index}.png"},
            }} for index in range(2)
        ]}))
        request = MagicMock()
        request.__aenter__ = AsyncMock(return_value=response)
        request.__aexit__ = AsyncMock(return_value=None)
        http = SimpleNamespace(closed=False, get=MagicMock(return_value=request), close=AsyncMock())
        client._session = http
        client.download_still = AsyncMock()
        archive = SimpleNamespace(enqueue_debug=Mock())
        context = replace(gif_context(), bot=SimpleNamespace(gif_client=client, agent_model=model),
                          transcript_archive=archive)
        context.source_message.id = 300
        context.source_message.reply = AsyncMock(return_value=SimpleNamespace(id=301))
        try:
            with patch.dict(ITEM_BY_ID, ts_lowkey_tuff={**ITEM_BY_ID['ts_lowkey_tuff'],
                            'media': 'https://giphy.com/gifs/synthetic-1'}):
                answer = await AgentService(model).answer(
                    question="The imaginary crown melted again.", local_context="Synthetic mutual banter.", context=context,
                )
            self.assertEqual(answer, "")
            url = "https://giphy.com/gifs/synthetic-1"
            self.assertEqual(context.state.gif_url, url)
            http.get.assert_not_called()
            client.download_still.assert_not_awaited()
            self.assertEqual(len(requests), 2)  # Routing and native output.
            record = archive.enqueue_debug.call_args.args[1]
            self.assertEqual([item["capability"] for item in record.tool_calls], ["find_gif"])
            self.assertEqual(record.plans[0]["plan"]["reply_form"], "text")
            self.assertTrue(record.plans[0]["check"]["ok"])
            delivery = AgentDelivery()
            surface = AgentDeliveryMixin()
            surface.transcript_archive = None
            surface._send_response = AsyncMock()
            with (
                patch("elbow_helper.features.agent.delivery.assert_answer_scope", AsyncMock()),
                patch("elbow_helper.features.agent.delivery.assert_answer_scope", AsyncMock(return_value=True)),
            ):
                await surface.send_response(context.source_message, answer, None, context=context, delivery=delivery)
            context.source_message.reply.assert_awaited_once()
            self.assertEqual(context.source_message.reply.await_args.args, (url,))
            self.assertFalse(context.source_message.reply.await_args.kwargs["allowed_mentions"].replied_user)
            self.assertEqual(delivery.text_parts, [url])
            self.assertTrue(delivery.complete)
            surface._send_response.assert_not_awaited()
        finally:
            await client.close()
            await model.close()

    async def test_reaction_only_choice_keeps_reactions_and_gets_one_plain_text_line(self):
        context = _context()
        context.source_message.id = 300
        context.source_message.add_reaction = AsyncMock()
        # Report synthetic usage so catalogue growth does not turn this recovery
        # check into a test of the conservative missing-usage budget.
        session = _AgentSession([replace(social_step("", form="text_reactions", reactions=["\U0001f44d"]),
                                        usage=AgentUsage(100, 10, 0, 100)),
                                 AgentStep("Synthetic acknowledgement.", (), AgentUsage())])
        answer = await AgentService(_AgentModel(session, route="social")).answer(
            question="heh", local_context="Synthetic shared joke.", context=context,
        )
        self.assertEqual(answer, "Synthetic acknowledgement.")
        self.assertEqual(context.state.reactions.emojis, ["\U0001f44d"])
        self.assertEqual(len(session.calls), 2)
        self.assertFalse(session.calls[1][1])
        self.assertEqual(session.tool_replacements, [()])
        self.assertEqual(session.efforts, [AgentReasoningEffort.LOW] * 2)
        self.assertIn("Reply to this message in one line", session.instructions[1])
        context.source_message.add_reaction.assert_awaited_once_with("\U0001f44d")
        delivery = AgentDelivery()
        surface = AgentDeliveryMixin()
        surface._send_response = AsyncMock()
        with patch("elbow_helper.features.agent.delivery.require_evidence_access", AsyncMock()):
            await surface.send_response(context.source_message, answer, None, context=context, delivery=delivery)
        surface._send_response.assert_awaited_once()

    async def test_unusable_social_outputs_get_one_text_only_round(self):
        for invalid in (
            AgentStep("", (), AgentUsage()),
            AgentStep("", (AgentToolCall("bad", "submit_social_reply", "{"),), AgentUsage()),
            AgentStep("", (AgentToolCall("bad", "submit_social_reply", "[]"),), AgentUsage()),
            social_step("", meme_id="", reactions=[]),
            social_step(text=42, meme_id=None, reactions=[None]),
        ):
            with self.subTest(invalid=invalid):
                session = _AgentSession([invalid, AgentStep("Synthetic new line.", (), AgentUsage())])
                answer = await AgentService(_AgentModel(session, route="social")).answer(
                    question="Synthetic shared joke", local_context="", context=_context(),
                )
                self.assertEqual(answer, "Synthetic new line.")
                self.assertEqual(len(session.calls), 2)
                self.assertFalse(session.calls[1][1])
                self.assertEqual(session.tool_replacements, [()])
                self.assertEqual(session.efforts, [AgentReasoningEffort.LOW] * 2)
                self.assertEqual({item.call_id for item in session.calls[1][0]},
                                 {item.call_id for item in invalid.tool_calls})

    async def test_wrong_forms_missing_queries_and_plain_prose_keep_valid_text(self):
        for invalid in (social_step("Synthetic salvaged line.", reply_form=None),
                        social_step("Synthetic salvaged line.", form="wrong"),
                        social_step("Synthetic salvaged line.", form="gif", meme_id=""),
                        social_step("Synthetic salvaged line.", meme_id=""),
                        AgentStep("Synthetic salvaged line.", (), AgentUsage())):
            client = candidate_client(AsyncMock())
            context = replace(gif_context(), bot=SimpleNamespace(gif_client=client))
            session = _AgentSession([invalid])
            answer = await AgentService(_AgentModel(session, route="social")).answer(
                question="Synthetic shared joke", local_context="", context=context,
            )
            self.assertEqual(answer, "Synthetic salvaged line.")
            self.assertEqual(len(session.calls), 1)
            client.search_candidates.assert_not_awaited()

    async def test_bad_reaction_fields_do_not_discard_valid_reactions_or_text(self):
        context = _context()
        context.source_message.id = 300
        context.source_message.add_reaction = AsyncMock()
        session = _AgentSession([social_step("Synthetic line.", form="wrong",
                                           reactions=[None, "\U0001f44d", "", 42])])
        answer = await AgentService(_AgentModel(session, route="social")).answer(
            question="Synthetic shared joke", local_context="", context=context,
        )
        self.assertEqual(answer, "Synthetic line.")
        self.assertEqual(len(session.calls), 1)
        context.source_message.add_reaction.assert_awaited_once_with("\U0001f44d")

    async def test_provider_failure_uses_existing_message_and_keeps_salvaged_reactions(self):
        for error in (TextGenerationError("Synthetic provider failure"), TimeoutError()):
            for initial in (True, False):
                with self.subTest(error=type(error).__name__, initial=initial):
                    context = _context()
                    context.source_message.id = 300
                    context.source_message.add_reaction = AsyncMock()
                    session = _AgentSession([])
                    steps = [error] if initial else [
                        replace(social_step("", form="text_reactions", reactions=["\U0001f44d"]),
                                usage=AgentUsage(100, 10, 0, 100)), error,
                    ]
                    session.advance = AsyncMock(side_effect=steps)
                    answer = await AgentService(_AgentModel(session, route="social")).answer(
                        question="Synthetic shared joke", local_context="", context=context,
                    )
                    self.assertEqual(answer, FAILURE_MESSAGE if isinstance(error, TextGenerationError)
                                     else "That reply ran out of time.")
                    self.assertEqual(session.advance.await_count, 1 if initial else 2)
                    self.assertEqual(context.source_message.add_reaction.await_count, 0 if initial else 1)
                    if not initial:
                        self.assertFalse(session.advance.await_args.kwargs["allow_tools"])
                        self.assertEqual(session.tool_replacements, [()])

    async def test_reactions_cannot_satisfy_empty_answer_or_skip_message_delivery(self):
        context = _context()
        context.source_message.id = 300
        context.state.reactions.count = 1
        context.state.reactions.emojis = ["\U0001f44d"]
        service = AgentService(None)
        with patch.object(service, "_answer", AsyncMock(return_value="")):
            answer = await service.answer(question="Synthetic chat", local_context="", context=context)
        self.assertEqual(answer, AGENT_ANSWER_UNFINISHED)
        surface = AgentDeliveryMixin()
        surface._send_response = AsyncMock()
        await surface.send_response(context.source_message, "", None, context=context)
        surface._send_response.assert_awaited_once()

    async def test_failed_native_outputs_report_every_call_id_before_text_fallback(self):
        client = candidate_client(AsyncMock(return_value=None))
        context = replace(_context(), bot=SimpleNamespace(gif_client=client, agent_model=client.agent_model))
        context.source_message.id = 300
        context.source_message.add_reaction = AsyncMock()
        session = _AgentSession([
            AgentStep("", (AgentToolCall("gif", "find_gif", '{"query":"synthetic astonishment"}'),
                           AgentToolCall("react", "react_to_request", '{"emoji":"unavailable"}')), AgentUsage()),
            social_step("Synthetic light acknowledgement."),
        ])
        answer = await AgentService(_AgentModel(session, route="social")).answer(
            question="The pretend throne broke.", local_context="", context=context,
        )
        self.assertEqual(answer, "Synthetic light acknowledgement.")
        self.assertEqual({result.call_id for result in session.calls[1][0]}, {"gif", "react"})
        self.assertIsNone(context.state.gif_url)
        context.source_message.add_reaction.assert_not_awaited()

    async def test_native_calls_respect_configuration_cooldown_research_and_schema(self):
        for mode in ("unconfigured", "cooldown", "research", "expanded", "invalid"):
            with self.subTest(mode=mode):
                client = candidate_client(AsyncMock(return_value="https://giphy.com/gifs/synthetic"))
                client.configured = mode != "unconfigured"
                context = replace(_context(), bot=SimpleNamespace(gif_client=client, agent_model=client.agent_model))
                if mode == "cooldown":
                    context = gif_context(context, recent=(True,))
                arguments = '{"query":""}' if mode == "invalid" else '{"query":"synthetic"}'
                steps = [AgentStep("", (AgentToolCall("native", "find_gif", arguments),), AgentUsage()),
                         social_step("Synthetic acknowledgement.") if mode not in ("research", "expanded")
                         else AgentStep("Synthetic acknowledgement.", (), AgentUsage())]
                if mode == "expanded":
                    steps.insert(0, AgentStep("", (AgentToolCall("expand", "load_research_context", "{}"),), AgentUsage()))
                session = _AgentSession(steps)
                model = _AgentModel(session, route="research" if mode == "research" else "social")
                answer = await AgentService(model).answer(question="Synthetic exchange", local_context="", context=context)
                self.assertEqual(answer, "Synthetic acknowledgement.")
                client.search_candidates.assert_not_awaited()
                self.assertIsNone(context.state.gif_url)
                if mode in ("unconfigured", "cooldown", "research"):
                    self.assertNotIn("find_gif", {definition.name for definition in model.request["tools"]})
                if mode == "expanded":
                    self.assertEqual([definition.name for definition in session.tool_replacements[0]],
                                     ["submit_request_plan"])

    def test_native_plan_ids_remain_distinct_for_later_calls(self):
        first = request_plan(AgentStep("", (AgentToolCall("one", "find_gif", '{"query":"synthetic"}'),), AgentUsage()),
                             {"find_gif"})
        second = request_plan(AgentStep("", (AgentToolCall("two", "find_gif", '{"query":"synthetic"}'),), AgentUsage()),
                              {"find_gif"})
        self.assertNotEqual(first["steps"][0]["id"], second["steps"][0]["id"])
