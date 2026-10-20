"""Synthetic model and delivery checks for channel-first knowledge answers."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.roles import AGENT_TESTER_ROLE_ID
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.conversation.state import Conversation
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.knowledge.answers import needs_private_knowledge
from elbow_helper.features.agent.knowledge.store import KnowledgeCatalog
from elbow_helper.features.agent.knowledge.tools import search_approved_knowledge, _section_id
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.wording import ACTION_PRIVATE_ANSWER, AGENT_ANSWER_UNFINISHED, FAILURE_MESSAGE
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage
from tests.features.agent.knowledge import test_search_scope
from tests.features.agent.test_agent_audiences import Member, Role
from tests.features.agent_plan_helpers import plan_call


class Session:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []
        self.before_call = None

    async def advance(self, results=(), **options):
        self.calls.append((tuple(results), options))
        if self.before_call:
            self.before_call(len(self.calls))
        return self.steps.pop(0)

    def replace_tools(self, tools):
        pass


class ChannelFirstTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        test_search_scope.SearchScopeTests.setUp(self)
        self.guild.name = "Synthetic guild"
        self.channel.name = "Synthetic channel"
        object.__setattr__(self.requester, "display_name", "Asker")

    async def answer(self, first, *, private=None, titles=None):
        if first == '{"needs_restricted_knowledge":true}':
            selected = [section for section in self.catalog.active_sections
                        if section.visibility != "public" and (titles is None or section.title in titles)]
            first = json.dumps({"needs_restricted_knowledge": True,
                                "section_ids": [_section_id(section) for section in selected]})
        arguments = {"titles": titles} if titles else {"query": "Brown Elbow"}
        steps = [AgentStep("", (plan_call(AgentToolCall(
            "knowledge", "search_approved_knowledge", json.dumps(arguments),
        )),), AgentUsage(100, 10)), AgentStep(first, (), AgentUsage(100, 10))]
        if private is not None:
            steps.append(AgentStep(private, (), AgentUsage(100, 10)))
        self.session = Session(steps)
        self.model = SimpleNamespace(create_agent_session=lambda **kw: self.capture(kw))
        return await AgentService(self.model).answer(
            question="Analyze Brown Elbow history", local_context="", context=self.context,
        )

    def capture(self, request):
        self.request = request
        return self.session

    async def deliver(self, answer, conversation=None):
        await self.delivery.send_response(
            self.message, answer, None, context=self.context, conversation=conversation,
        )
        return self.message.reply.await_args.args[0]

    async def test_public_answer_with_private_matches_uses_only_one_answer_pass(self):
        answer = await self.answer("Brown Elbow has founding history.")
        self.assertEqual(len(self.session.calls), 2)
        self.assertEqual(await self.deliver(answer), answer)
        self.assertFalse(self.context.state.private_knowledge_used)
        payload = json.loads(self.session.calls[1][0][0].content)
        sections = payload["results"]["knowledge"]["sections"]
        self.assertEqual(len(sections), 3)
        for section in sections:
            if section["title"] != "Clan family lore":
                self.assertEqual(set(section), {"title", "section_id", "available_privately"})
        self.assertNotIn(self.catalog.active_sections[1].body, str(self.session.calls))

    async def test_structured_request_adds_exactly_one_private_pass_and_preserves_logs(self):
        answer = await self.answer('{"needs_restricted_knowledge":true}', private="Private policy answer.")
        self.assertEqual(len(self.session.calls), 3)
        self.assertTrue(self.context.state.private_knowledge_used)
        self.assertEqual(self.context.state.required_access, {"lead", "recruiter"})
        self.assertNotIn("body", json.dumps([
            s for s in json.loads(self.session.calls[1][0][0].content)["results"]["knowledge"]["sections"]
            if s.get("available_privately")
        ]))
        final = self.session.calls[2][1]
        self.assertFalse(final["allow_tools"])
        self.assertIn(self.catalog.active_sections[1].body[:4000], final["continuation_instruction"])
        with self.assertLogs("elbow_helper.features.agent.disclosure", "INFO") as logs:
            self.assertEqual(await self.deliver(answer), ACTION_PRIVATE_ANSWER)
        self.assertIn("failed_requirement=lead reader_kind=role_group", logs.output[0])
        self.assertEqual(self.message.reply.await_args.kwargs["view"].parts[0], answer)
        self.assertTrue(any('private-knowledge-answer' in item for item in self.context.state.evidence))

    async def test_unused_private_matches_do_not_force_a_private_answer(self):
        answer = await self.answer("No public evidence.", private="Private policy answer.",
                                   titles=["Alliance policies"])
        self.assertEqual(len(self.session.calls), 2)
        self.assertEqual(await self.deliver(answer), "No public evidence.")

    async def test_no_private_match_means_no_extra_pass(self):
        answer = await self.answer("Public answer.", titles=["Clan family lore"])
        self.assertEqual(len(self.session.calls), 2)
        self.assertEqual(await self.deliver(answer), answer)

    async def test_selected_body_only_and_explicit_post_reuses_exact_covered_source(self):
        answer = await self.answer('{"needs_restricted_knowledge":true}', private="Policy answer.",
                                   titles=["Alliance policies"])
        self.assertNotIn('"title": "Returning applicants"', self.session.calls[-1][1]["continuation_instruction"])
        conversation = Conversation(1, self.channel.id)
        self.delivery._conversations = SimpleNamespace(register_reply=lambda *_: None)
        sent = self.message.reply.return_value
        sent.edit = AsyncMock(return_value=sent)
        self.assertEqual(await self.deliver(answer, conversation), ACTION_PRIVATE_ANSWER)

        view = self.message.reply.await_args.kwargs["view"]
        interaction = SimpleNamespace(id=801, user=self.requester, response=SimpleNamespace(
            send_message=AsyncMock(), defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await view.open_result(interaction)
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        await view.post_here(interaction)
        self.assertEqual(sent.edit.await_args.kwargs["content"], "Policy answer.")
        self.assertTrue(view.expired)
        self.assertEqual(conversation.answer_disclosures[2].knowledge_sources,
                         {_section_id(self.catalog.active_sections[1])})
        self.context = replace(self.context, state=AgentTurnState())
        self.message.reply.reset_mock()
        answer = await self.answer('{"needs_restricted_knowledge":true}', private="Policy follow-up.",
                                   titles=["Alliance policies"])
        with self.assertLogs("elbow_helper.features.agent.delivery", "INFO") as logs:
            self.assertEqual(await self.deliver(answer, conversation), "Policy follow-up.")
        self.assertTrue(any("visibility=public reason=prior_disclosure_reused" in line for line in logs.output), logs.output)
        self.assertNotIn("view", self.message.reply.await_args.kwargs)

        # A different section with the same role requirement still needs consent.
        self.catalog = replace(self.catalog, active_sections=(*self.catalog.active_sections,
            replace(self.catalog.active_sections[1], title="Different policy", body="Different restricted evidence.")))
        self.context = replace(self.context, state=AgentTurnState())
        self.message.reply.reset_mock()
        answer = await self.answer('{"needs_restricted_knowledge":true}', private="Different policy answer.",
                                   titles=["Different policy"])
        self.assertEqual(await self.deliver(answer, conversation), ACTION_PRIVATE_ANSWER)

    async def test_ambiguous_boolean_signal_gets_one_selection_repair_without_opening_unneeded_bodies(self):
        selected = _section_id(self.catalog.active_sections[1])
        self.session = Session([
            AgentStep("", (plan_call(AgentToolCall("knowledge", "search_approved_knowledge", '{"query":"Brown Elbow"}')),), AgentUsage(100, 10)),
            AgentStep('{"needs_restricted_knowledge":true}', (), AgentUsage(100, 10)),
            AgentStep(json.dumps({"needs_restricted_knowledge": True, "section_ids": [selected]}), (), AgentUsage(100, 10)),
            AgentStep("Selected policy answer.", (), AgentUsage(100, 10)),
        ])
        answer = await AgentService(SimpleNamespace(create_agent_session=lambda **kw: self.capture(kw))).answer(
            question="Analyze Brown Elbow history", local_context="", context=self.context)
        self.assertEqual(answer, "Selected policy answer.")
        self.assertEqual(len(self.session.calls), 4)
        selection = self.session.calls[2][1]["continuation_instruction"]
        self.assertNotIn(self.catalog.active_sections[1].body, selection)
        self.assertEqual(self.context.state.required_access, {"lead"})
        self.assertEqual(self.context.state.knowledge_sources, {selected})
        self.assertNotIn('"title": "Returning applicants"', self.session.calls[3][1]["continuation_instruction"])

    async def test_invalid_section_selection_repairs_once_and_never_opens_unknown_bodies(self):
        selected = _section_id(self.catalog.active_sections[1])
        for invalid in ([], ["unknown"], [{}], "unknown", None):
            for repaired in ([selected], [{}]):
                with self.subTest(invalid=invalid, repaired=repaired):
                    self.context = replace(self.context, state=AgentTurnState())
                    self.session = Session([
                        AgentStep("", (plan_call(AgentToolCall(
                            "knowledge", "search_approved_knowledge", '{"query":"Brown Elbow"}',
                        )),), AgentUsage(100, 10)),
                        AgentStep(json.dumps({"needs_restricted_knowledge": True,
                                              "section_ids": invalid}), (), AgentUsage(100, 10)),
                        AgentStep(json.dumps({"needs_restricted_knowledge": True,
                                              "section_ids": repaired}), (), AgentUsage(100, 10)),
                        AgentStep("Selected policy answer.", (), AgentUsage(100, 10)),
                    ])
                    answer = await AgentService(SimpleNamespace(
                        create_agent_session=lambda **kw: self.capture(kw),
                    )).answer(question="Analyze Brown Elbow history", local_context="", context=self.context)
                    if repaired == [selected]:
                        self.assertEqual(answer, "Selected policy answer.")
                        self.assertEqual(len(self.session.calls), 4)
                        self.assertEqual(self.context.state.knowledge_sources, {selected})
                    else:
                        self.assertEqual(answer, FAILURE_MESSAGE)
                        self.assertEqual(len(self.session.calls), 3)
                        self.assertFalse(self.context.state.private_knowledge_used)
                        self.assertFalse(self.context.state.knowledge_sources)
                    selection = self.session.calls[2][1]["continuation_instruction"]
                    self.assertNotIn(self.catalog.active_sections[1].body, selection)

    async def test_malformed_internal_signals_repair_before_body_access_and_deliver_no_protocol(self):
        self.catalog = replace(self.catalog, active_sections=tuple(
            replace(section, body=f"Brown Elbow synthetic {section.visibility} section body.")
            if section.visibility != "public" else section for section in self.catalog.active_sections))
        selected = _section_id(self.catalog.active_sections[1])
        signals = (
            {"needs_restricted_knowledge": True, "section_ids": [selected], "reason": "internal_reason_marker"},
            {"needs_restricted_knowledge": True, "section_ids": {"unexpected": selected}},
            {"needs_restricted_knowledge": "true", "section_ids": [selected]},
        )
        for signal in signals:
            for repaired in (True, False):
                with self.subTest(signal=signal, repaired=repaired):
                    self.context = replace(self.context, state=AgentTurnState())
                    self.message.reply.reset_mock()
                    self.session = Session([
                        AgentStep("", (plan_call(AgentToolCall("knowledge", "search_approved_knowledge", '{"query":"Brown Elbow"}')),), AgentUsage(100, 10)),
                        AgentStep(json.dumps(signal), (), AgentUsage(100, 10)),
                        AgentStep(json.dumps({"needs_restricted_knowledge": True, "section_ids": [selected]}
                                             if repaired else signal), (), AgentUsage(100, 10)),
                        AgentStep("Selected policy answer.", (), AgentUsage(100, 10)),
                    ])
                    answer = await AgentService(SimpleNamespace(create_agent_session=lambda **kw: self.capture(kw))).answer(
                        question="Analyze Brown Elbow history", local_context="", context=self.context)
                    shown = await self.deliver(answer)
                    self.assertNotIn("needs_restricted_knowledge", shown)
                    self.assertNotIn("internal_reason_marker", shown)
                    self.assertNotIn(self.catalog.active_sections[1].body, str(self.session.calls[:3]))
                    self.assertNotIn(self.catalog.active_sections[2].body, str(self.session.calls))
                    if repaired:
                        self.assertEqual(shown, ACTION_PRIVATE_ANSWER)
                        self.assertEqual(len(self.session.calls), 4)
                        self.assertEqual(self.context.state.knowledge_sources, {selected})
                        view = self.message.reply.await_args.kwargs["view"]
                        interaction = SimpleNamespace(user=self.requester, response=SimpleNamespace(send_message=AsyncMock()))
                        await view.open_result(interaction)
                        self.assertEqual(interaction.response.send_message.await_args.args[0], "Selected policy answer.")
                    else:
                        self.assertEqual(shown, FAILURE_MESSAGE)
                        self.assertEqual(len(self.session.calls), 3)
                        self.assertFalse(self.context.state.private_knowledge_used)
                        self.assertFalse(self.context.state.knowledge_sources)

    async def test_removed_role_blocks_private_view_and_post_here(self):
        from elbow_helper.features.agent.wording import ACTION_RESULT_EXPIRED
        answer = await self.answer('{"needs_restricted_knowledge":true}', private="Policy answer.",
                                   titles=["Alliance policies"])
        await self.deliver(answer)
        view = self.message.reply.await_args.kwargs["view"]
        view.message.edit = AsyncMock()
        self.members[0] = Member(2, (Role(AGENT_TESTER_ROLE_ID),))
        for callback in (view.open_result, view.post_here):
            interaction = SimpleNamespace(user=self.members[0], response=SimpleNamespace(send_message=AsyncMock()))
            await callback(interaction)
            interaction.response.send_message.assert_awaited_with(ACTION_RESULT_EXPIRED, ephemeral=True)

    async def test_private_pass_receives_the_end_of_a_long_section(self):
        body = "Policy detail. " * 650 + "Important exception at the end."
        self.catalog = replace(self.catalog, active_sections=(
            replace(self.catalog.active_sections[1], body=body),
        ))
        answer = await self.answer('{"needs_restricted_knowledge":true}', private="Policy answer.",
                                   titles=["Alliance policies"])
        self.assertIn(body, self.session.calls[2][1]["continuation_instruction"])
        self.assertNotIn(body, str(self.session.calls[:2]))
        self.assertEqual(await self.deliver(answer), ACTION_PRIVATE_ANSWER)
        evidence = json.loads(self.context.state.evidence[-1])
        self.assertTrue(evidence["result_complete"])
        self.assertEqual(json.loads(evidence["result"])["sections"][0]["body"], body)

    async def test_private_section_reopens_keep_one_complete_body(self):
        await search_approved_knowledge(self.context, {"titles": ["Alliance policies"]})
        await search_approved_knowledge(self.context, {
            "titles": ["Alliance policies"], "section_offset": 4000,
        })
        page, = self.context.state.private_knowledge.values()
        self.assertEqual(page["body"], self.catalog.active_sections[1].body)
        self.assertEqual(page["section_offset"], 0)
        self.assertFalse(page["truncated"])

    async def test_private_evidence_limit_stops_without_sending_a_clipped_body(self):
        with patch("elbow_helper.features.agent.knowledge.answers.limits.MAX_TOOL_RESULT_CHARACTERS", 1000):
            answer = await self.answer('{"needs_restricted_knowledge":true}')
        self.assertEqual(len(self.session.calls), 2)
        self.assertEqual(answer, AGENT_ANSWER_UNFINISHED)
        self.assertFalse(self.context.state.private_knowledge_used)
        self.assertEqual(await self.deliver(answer), AGENT_ANSWER_UNFINISHED)
        self.assertFalse(any('private-knowledge-answer' in item for item in self.context.state.evidence))

    async def test_repeated_private_title_cannot_be_delivered_publicly(self):
        answer = await self.answer("See **ALLIANCE**\nPOLICIES for that.")
        self.assertEqual(len(self.session.calls), 2)
        self.assertFalse(self.context.state.private_knowledge_used)
        conversation = Conversation(1, self.channel.id)
        conversation.remember_answer_disclosure(2, self.channel.id, {self.channel.id}, {"lead"})
        self.delivery._conversations = SimpleNamespace(register_reply=lambda *_: None)
        self.assertEqual(await self.deliver(answer, conversation), ACTION_PRIVATE_ANSWER)
        self.assertIn("lead", self.context.state.required_access)

    async def test_private_title_inside_a_different_word_does_not_hide_the_answer(self):
        self.catalog = replace(self.catalog, active_sections=(
            self.catalog.active_sections[0], replace(self.catalog.active_sections[1], title="Core"),
        ))
        answer = await self.answer("Scores improved.")
        self.assertTrue(self.context.state.private_knowledge)
        self.assertEqual(await self.deliver(answer), answer)

    async def test_title_already_available_to_the_channel_does_not_hide_the_answer(self):
        self.catalog = replace(self.catalog, active_sections=(
            replace(self.catalog.active_sections[0], title="Shared title"),
            replace(self.catalog.active_sections[1], title="Shared title"),
        ))
        answer = await self.answer("Shared title describes the public founding history.")
        self.assertTrue(self.context.state.private_knowledge)
        self.assertFalse(self.context.state.required_access)
        self.assertEqual(await self.deliver(answer), answer)

    async def test_direct_reply_tool_cannot_post_a_private_title(self):
        await self.answer("Public history answer.")
        await self.delivery.send_post_reply(
            self.message, "See Alliance policies.", None, self.context, None,
        )
        self.assertEqual(self.message.reply.await_args.args[0], ACTION_PRIVATE_ANSWER)

    async def test_private_title_cannot_send_an_accompanying_public_gif(self):
        answer = await self.answer("See Alliance policies.")
        self.context.state.gif_url = "https://media.tenor.com/example.gif"
        self.delivery._send_gif = AsyncMock()
        self.assertEqual(await self.deliver(answer), ACTION_PRIVATE_ANSWER)
        self.delivery._send_gif.assert_not_awaited()

    async def test_first_pass_evidence_retains_title_access_without_private_bodies(self):
        await self.answer("Public history answer.")
        record, = map(json.loads, self.context.state.evidence)
        self.assertEqual(set(record["required_access"]), {"lead", "recruiter"})
        payload = json.loads(record["result"])
        self.assertTrue(any(section.get("available_privately") for section in payload["sections"]))
        self.assertNotIn(self.catalog.active_sections[1].body, record["result"])

    async def test_retained_report_keeps_the_private_title_requirements(self):
        await self.answer("Public history answer.")

        async def report(context, arguments):
            context.state.reports["synthetic"] = SimpleNamespace(report_id="synthetic", retained_bytes=1)
            return {"report_id": "synthetic"}

        await AgentService.execute_tool(
            name="synthetic_report", handler=report, arguments={}, context=self.context,
        )
        self.assertEqual(self.context.state.report_access_requirements["synthetic"], {"lead", "recruiter"})
        self.assertFalse(self.context.state.required_access)

    async def test_model_authored_artifact_keeps_private_title_access(self):
        registry = build_agent_tools()
        registry["prepare_spreadsheet"] = replace(
            registry["prepare_spreadsheet"], handler=AsyncMock(return_value={"attachment_prepared": True}),
        )
        sheet = {"title": "Alliance policies", "sheets": [
            {"name": "Notes", "columns": ["Topic"], "rows": [["Alliance policies"]]},
        ]}
        session = Session([
            AgentStep("", (plan_call(AgentToolCall("knowledge", "search_approved_knowledge", "{}")),),
                      AgentUsage(100, 10)),
            AgentStep("", (plan_call(AgentToolCall("sheet", "prepare_spreadsheet", json.dumps(sheet)),
                                     output="prepare_spreadsheet"),), AgentUsage(100, 10)),
            AgentStep("Prepared.", (), AgentUsage(100, 10)),
        ])
        model = SimpleNamespace(create_agent_session=lambda **_: session)
        with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry):
            answer = await AgentService(model).answer(
                question="Analyze Brown Elbow history", local_context="", context=self.context,
            )
        self.assertEqual(self.context.state.required_access, {"lead", "recruiter"})
        self.assertEqual(await self.deliver(answer), ACTION_PRIVATE_ANSWER)

    async def test_unauthorized_requester_never_gets_titles_in_model_input(self):
        self.members[0] = Member(2, (Role(AGENT_TESTER_ROLE_ID),))
        answer = await self.answer("Public answer.")
        serialized = json.dumps(self.request, default=str) + str(self.session.calls)
        self.assertNotIn("Alliance policies", serialized)
        self.assertNotIn("Returning applicants", serialized)
        self.assertNotIn("available_privately", str(self.session.calls))
        self.assertEqual(len(self.session.calls), 2)
        self.assertEqual(await self.deliver(answer), answer)

    async def test_always_loaded_restricted_text_cannot_bypass_first_pass(self):
        self.catalog = KnowledgeCatalog("synthetic", tuple(
            replace(section, always_loaded=True) for section in self.catalog.active_sections
        ))
        await self.answer("Public answer.")
        self.assertIn("### Clan family lore", self.request["system_prompt"])
        self.assertNotIn("### Alliance policies", self.request["system_prompt"])
        self.assertNotIn(self.catalog.active_sections[1].body, self.request["system_prompt"])
        self.assertFalse(self.context.state.required_access)

    async def test_private_pass_cannot_reuse_prior_public_disclosure(self):
        answer = await self.answer('{"needs_restricted_knowledge":true}', private="Private answer.")
        conversation = Conversation(1, self.channel.id)
        conversation.remember_answer_disclosure(2, self.channel.id, {self.channel.id}, {"lead", "recruiter"})
        self.delivery._conversations = SimpleNamespace(register_reply=lambda *_: None)
        self.assertEqual(await self.deliver(answer, conversation), ACTION_PRIVATE_ANSWER)
        # Current audience proof can authorize delivery independently of consent.
        self.channel.viewers.remove(self.reader.id)
        self.assertEqual(await self.deliver(answer), answer)

    async def test_always_loaded_public_material_can_answer_without_private_pass(self):
        self.catalog = KnowledgeCatalog("synthetic", tuple(
            replace(section, always_loaded=True) for section in self.catalog.active_sections
        ))
        answer = await self.answer("Public answer.", titles=["Alliance policies"])
        self.assertEqual(len(self.session.calls), 2)
        self.assertEqual(await self.deliver(answer), answer)

    async def test_private_notes_retain_role_checks_before_any_later_model_call(self):
        original_capture = self.capture

        def capture(request):
            session = original_capture(request)
            def revoke(call):
                if call == 2:
                    self.members[0] = Member(2, (Role(AGENT_TESTER_ROLE_ID),))
            session.before_call = revoke
            return session
        self.capture = capture
        with self.assertRaises(AgentAccessLost):
            await self.answer('{"needs_restricted_knowledge":true}', private="Never generated.")
        self.assertEqual(len(self.session.calls), 2)
        self.assertFalse(self.context.state.private_knowledge_used)

    async def test_prose_does_not_request_private_bodies(self):
        await self.answer("I need restricted knowledge to answer that.")
        self.assertEqual(len(self.session.calls), 2)
        self.assertFalse(self.context.state.private_knowledge_used)

    async def test_second_pass_cannot_request_a_third_pass(self):
        answer = await self.answer('{"needs_restricted_knowledge":true}',
                                   private='{"needs_restricted_knowledge":true}')
        self.assertEqual(len(self.session.calls), 3)
        self.assertNotIn("needs_restricted_knowledge", answer)
        self.assertEqual(await self.deliver(answer), ACTION_PRIVATE_ANSWER)

    async def test_private_pass_cannot_execute_another_tool(self):
        original_capture = self.capture

        def capture(request):
            session = original_capture(request)
            session.steps[-1] = replace(session.steps[-1], tool_calls=(
                AgentToolCall("extra", "search_approved_knowledge", "{}"),
            ))
            return session

        self.capture = capture
        answer = await self.answer('{"needs_restricted_knowledge":true}', private="")
        self.assertEqual(len(self.session.calls), 3)
        self.assertEqual(answer, FAILURE_MESSAGE)
        self.assertEqual(await self.deliver(answer), ACTION_PRIVATE_ANSWER)

    async def test_private_pass_output_limit_cannot_start_another_pass(self):
        original_capture = self.capture

        def capture(request):
            session = original_capture(request)
            session.steps[-1] = replace(session.steps[-1], output_limit_reached=True)
            return session

        self.capture = capture
        answer = await self.answer('{"needs_restricted_knowledge":true}', private="Incomplete answer")
        self.assertEqual(len(self.session.calls), 3)
        self.assertEqual(answer, AGENT_ANSWER_UNFINISHED)
        self.assertEqual(await self.deliver(answer), ACTION_PRIVATE_ANSWER)

    def test_internal_signal_recognition_does_not_authorize_malformed_requests(self):
        self.assertTrue(needs_private_knowledge('{"needs_restricted_knowledge":true}'))
        for answer in ('{"needs_restricted_knowledge":1}',
                       '{"needs_restricted_knowledge":"true"}',
                       '{"needs_restricted_knowledge":false}',
                       '{"needs_restricted_knowledge":true,"answer":"x"}',
                       '{"needs_restricted_knowledge":true,',
                       '```json\n{"needs_restricted_knowledge":true}\n```'):
            self.assertTrue(needs_private_knowledge(answer))
        for answer in ('true', 'I need restricted knowledge.', '{"answer":"Public answer."}'):
            self.assertFalse(needs_private_knowledge(answer))
