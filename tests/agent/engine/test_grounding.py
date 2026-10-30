"""Source integrity and useful recovery; semantic quality is evaluated with a real model."""

from types import SimpleNamespace
import json
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.engine.grounding import (
    GROUNDING_GAPS, GroundingError, ground_answer, partial_answer,
)
from elbow_helper.agent.engine.flow import AnswerFlow
from elbow_helper.agent.engine.rounds import AgentLimitReached
from elbow_helper.agent.engine.tool_call import bound_tool_result
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.capability_contract import CapabilityContract
from elbow_helper.agent.models import RegisteredAgentTool
from elbow_helper.agent.plan.results import compact_result
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage, AgentToolDefinition
from agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan, _step


MESSAGE = {"message_id": 101, "author_id": 202, "author": "Member Alpha",
           "content": "I am not very social, but I plan to stay.",
           "source": "https://discord.com/channels/3/91/101"}


class GroundingTests(unittest.TestCase):
    def test_numeric_citations_render_linked_names_before_source_quotation(self):
        for citation in ('[[101]]', '[101]'):
            with self.subTest(citation=citation):
                answer = ground_answer('See ' + citation, {'read': {'messages': [MESSAGE]}})
                self.assertEqual(answer, f"See [Member Alpha]({MESSAGE['source']})")
        # Exact source quotations are escaped, not scanned for citation syntax.
        source = {**MESSAGE, 'content': 'A member wrote [[999]] as ordinary text.'}
        self.assertIn('999', ground_answer('[[message:101]]', [source]))

    def test_unknown_numeric_citation_uses_existing_repair_and_partial_recovery(self):
        with self.assertRaises(GroundingError):
            ground_answer('See [[999]].', [MESSAGE])
        recovered = partial_answer('Keep this advice. [[999]] [[101]]', [MESSAGE], GroundingError('unknown'))
        self.assertIn('Keep this advice.', recovered)
        self.assertIn(MESSAGE['source'], recovered)
        self.assertNotIn('[[', recovered)
        self.assertNotIn('999', recovered)

    def test_ordinary_language_and_recommendations_are_not_word_filtered(self):
        for answer in (
            'I would not push. They said "I am not very social", rather than raising a complaint.',
            "If anything needs saying, keep feedback optional.",
            "What is supported: they plan to stay. My recommendation is to leave it there.",
            "The history does not give a real name. I cannot infer one from a nickname.",
        ):
            with self.subTest(answer=answer):
                self.assertEqual(ground_answer(answer, {"read": {"messages": [MESSAGE]}}), answer)

    def test_binding_renders_actual_author_and_words_with_source(self):
        answer = ground_answer("[[message:101]]\nMy recommendation: leave feedback optional.",
                               {"read": {"messages": [MESSAGE]}})
        self.assertIn("Member Alpha", answer)
        self.assertIn(MESSAGE["source"], answer)
        self.assertIn("> " + MESSAGE["content"], answer)
        self.assertIn("My recommendation: leave feedback optional.", answer)

    def test_natural_quotation_and_source_can_appear_in_the_same_answer(self):
        answer = ground_answer(
            'On chatting: "I am not very social". [[message:101]]\n'
            'What is supported: they plan to stay. I would leave feedback optional.',
            {"read": {"messages": [MESSAGE]}},
        )
        self.assertIn('On chatting: "I am not very social".', answer)
        self.assertIn(MESSAGE["source"], answer)
        self.assertIn("I would leave feedback optional.", answer)

    def test_unknown_failed_and_excerpted_sources_do_not_qualify(self):
        for evidence in (
            {}, {"read": {"error": "denied", "messages": [MESSAGE]}},
            {"read": {"error": "", "messages": [MESSAGE]}},
            {"read": {"flags": {"status": "refused"}, "messages": [MESSAGE]}},
            {"read": {"excerpt": str(MESSAGE)}},
            {"read": {"messages": [{**MESSAGE, "source": "https://example.invalid/101"}]}},
            {"read": {"messages": [{**MESSAGE, "source": "https://discord.com/channels/3/91/999"}]}},
        ):
            with self.subTest(evidence=evidence), self.assertRaises(GroundingError):
                ground_answer("[[message:101]]", evidence)

    def test_malformed_binding_is_rejected(self):
        with self.assertRaisesRegex(GroundingError, "invalid_message_binding"):
            ground_answer("[[message:not-an-id]]", {"read": {"messages": [MESSAGE]}})

    def test_compacted_messages_work_and_conflicting_sources_fail(self):
        rows = [{**MESSAGE, "message_id": index,
                 "source": f"https://discord.com/channels/3/91/{index}"} for index in range(101, 111)]
        self.assertIn(MESSAGE["content"], ground_answer("[[message:101]]", compact_result(rows)))
        with self.assertRaises(GroundingError):
            ground_answer("[[message:101]]", [MESSAGE, {**MESSAGE, "author_id": 999}])

    def test_source_text_is_quoted_and_mentions_escaped(self):
        source = {**MESSAGE, "content": "First line\n@everyone **second line**"}
        answer = ground_answer("[[message:101]]", [source])
        self.assertNotIn("@everyone", answer)
        self.assertIn("\n> First line\n> ", answer)

    def test_partial_answer_keeps_prose_and_valid_referenced_sources(self):
        answer = partial_answer(
            "Do not preserve this unverified prose. [[message:101]] [[message:999]] " + MESSAGE["source"],
            {"read": {"messages": [MESSAGE]}}, GroundingError("unknown"),
        )
        self.assertIn(MESSAGE["content"], answer)
        self.assertIn(MESSAGE["source"], answer)
        self.assertNotIn(GROUNDING_GAPS["source"], answer)
        self.assertIn("unverified prose", answer)
        self.assertNotIn("999", answer)

    def test_failed_attribution_gets_a_qualifier_but_independent_advice_does_not(self):
        answer = partial_answer('They said "I will stay". [[message:999]] Keep feedback optional.',
                                {}, GroundingError("unknown"))
        self.assertIn('They said "I will stay".', answer)
        self.assertIn("Keep feedback optional.", answer)
        self.assertIn(GROUNDING_GAPS["source"], answer)
        answer = partial_answer("Keep feedback optional. [[message:999]]", {}, GroundingError("unknown"))
        self.assertEqual(answer, "Keep feedback optional.")

    def test_malformed_citation_is_removed_without_losing_prose(self):
        for citation in ("[[message:bad]]", "[[message:bad]", "[[message:bad"):
            with self.subTest(citation=citation):
                self.assertEqual(partial_answer("Keep feedback optional. " + citation, {},
                                                GroundingError("invalid")), "Keep feedback optional.")

    def test_oversized_results_keep_complete_citable_messages_and_table_rows(self):
        rows = [{**MESSAGE, "message_id": identity,
                 "source": f"https://discord.com/channels/3/91/{identity}"} for identity in range(101, 141)]
        for payload in ({"messages": rows, "search_is_exhaustive": True},
                        {"messages": compact_result(rows)}, rows,
                        {"result": {"messages": rows}}):
            with self.subTest(payload_type=type(payload).__name__):
                bounded = bound_tool_result(json.dumps(payload), 1200)
                self.assertLessEqual(len(bounded), 1200)
                view = json.loads(bounded)
                self.assertTrue(view["truncated"])
                self.assertNotIn("result_excerpt", view)
                self.assertIn(MESSAGE["content"], ground_answer("[[message:140]]", view))
                with self.assertRaises(GroundingError):
                    ground_answer("[[message:101]]", view)

    def test_an_oversized_single_record_is_omitted_whole(self):
        huge = {**MESSAGE, "content": "x" * 5000}
        view = json.loads(bound_tool_result(json.dumps({"messages": [huge]}), 400))
        self.assertEqual(view["messages"], [])
        self.assertEqual(view["truncated_collections"]["messages"],
                         {"total_records": 1, "returned_records": 0})

    def test_partial_answer_does_not_dump_unreferenced_or_failed_sources(self):
        for evidence in ({"read": {"messages": [MESSAGE]}},
                         {"read": {"error": "denied", "messages": [MESSAGE]}}):
            answer = partial_answer("[[message:999]]", evidence, GroundingError("unknown"))
            self.assertEqual(answer, GROUNDING_GAPS["source"])


class GroundingFlowTests(unittest.IsolatedAsyncioTestCase):
    async def answer(self, steps, *, payload=None, context=None, local_context=""):
        session = _Session(steps, [])
        registry = {"read_value": RegisteredAgentTool(
            AgentToolDefinition("read_value", "Read synthetic evidence.",
                                {"type": "object", "properties": {}}),
            AsyncMock(return_value=payload or {}), contract=CapabilityContract(),
        )}
        with patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=registry):
            answer = await AgentService(_Model(session)).answer(
                question="Would you push for feedback and why?", local_context=local_context, context=context or _context())
        return answer, session

    async def test_visible_local_message_is_citable_without_a_tool_or_repair(self):
        context = _context()
        context.state.local_messages = [MESSAGE]
        answer, session = await self.answer([
            AgentStep("Keep feedback optional. [[message:101]]", (), AgentUsage()),
        ], context=context, local_context="Visible local message: " + json.dumps(MESSAGE))
        self.assertIn("Keep feedback optional.", answer)
        self.assertIn(MESSAGE["source"], answer)
        self.assertEqual(len(session.calls), 1)

    async def test_recommendation_from_read_messages_needs_no_extra_model_call(self):
        draft = ('I would not push. They said "I am not very social" and plan to stay. '
                 'Keep feedback optional if anything needs saying. [[message:101]]')
        answer, session = await self.answer([
            _model_step(_plan([_step("read")])), AgentStep(draft, (), AgentUsage()),
        ], payload={"messages": [MESSAGE]})
        self.assertIn("I would not push.", answer)
        self.assertIn(MESSAGE["source"], answer)
        self.assertEqual(len(session.calls), 2)

    async def test_unknown_reference_is_repaired_once_without_discarding_recommendation(self):
        answer, session = await self.answer([
            _model_step(_plan([_step("read")])),
            AgentStep("Keep feedback optional. [[message:999]]", (), AgentUsage()),
            AgentStep("Keep feedback optional. [[message:101]]", (), AgentUsage()),
        ], payload={"messages": [MESSAGE]})
        self.assertIn("Keep feedback optional.", answer)
        self.assertIn(MESSAGE["content"], answer)
        self.assertEqual(len(session.calls), 3)
        self.assertFalse(session.calls[-1][1])
        self.assertIn("message_not_read_this_turn", session.instructions[-1])
        self.assertIn("Preserve the rest of the useful answer", session.instructions[-1])

    async def test_retained_evidence_is_not_a_current_turn_read(self):
        context = _context()
        context.state.grounding_evidence = {"old": {"messages": [MESSAGE]}}
        answer, _ = await self.answer([
            AgentStep("[[message:101]]", (), AgentUsage()),
            AgentStep("[[message:101]]", (), AgentUsage()),
        ], context=context)
        self.assertEqual(answer, GROUNDING_GAPS["source"])

    async def test_repeated_unknown_reference_reports_the_specific_gap(self):
        bad = AgentStep("[[message:999]]", (), AgentUsage())
        answer, session = await self.answer([bad, bad])
        self.assertEqual(answer, GROUNDING_GAPS["source"])
        self.assertEqual(len(session.calls), 2)

    async def test_failed_repair_keeps_real_sources_instead_of_generic_refusal(self):
        bad = AgentStep("Unverified prose. [[message:101]] [[message:999]]", (), AgentUsage())
        answer, _ = await self.answer([
            _model_step(_plan([_step("read")])), bad, bad,
        ], payload={"messages": [MESSAGE]})
        self.assertIn(MESSAGE["content"], answer)
        self.assertIn("Unverified prose", answer)

    async def test_repair_unavailable_still_keeps_read_sources(self):
        for error in (TimeoutError(), AgentLimitReached("Limit")):
            with self.subTest(error=type(error).__name__):
                context = _context()
                context.state.grounding_evidence = {"read": {"messages": [MESSAGE]}}
                flow = AnswerFlow(context=context, session=None, registry={}, budget=None,
                                  rounder=SimpleNamespace(advance=AsyncMock(side_effect=error)),
                                  runner=None, request_id=1)
                answer = await flow._grounded_reply("[[message:101]] [[message:999]]")
                self.assertIn(MESSAGE["content"], answer)
                self.assertIn(GROUNDING_GAPS["source"], answer)

    async def test_delivered_answer_links_a_message_the_model_saw_without_a_source_url(self):
        from elbow_helper.agent.families.model_view import family_model_view
        context = _context()
        row = {**MESSAGE, "channel_id": 91, "channel": "synthetic", "content_complete": True}
        view = family_model_view({"family": "read_conversation_evidence", "mode": "search",
                                  "rows": [row]})
        self.assertNotIn("source", view["rows"][0])
        context.state.grounding_evidence = {"read": compact_result(view)}
        flow = AnswerFlow(context=context, session=None, registry={}, budget=None,
                          rounder=None, runner=None, request_id=1)
        link = f"https://discord.com/channels/{context.guild.id}/91/101"
        answer = await flow._grounded_reply("See [[101]] and [[message:101]].")
        self.assertIn(f"[Member Alpha]({link})", answer)
        self.assertIn(f"([message]({link}))", answer)
        self.assertIn(MESSAGE["content"], answer)
        # Without the guild the row cannot be linked, so the reference fails closed.
        with self.assertRaises(GroundingError):
            ground_answer("[[message:101]]", context.state.grounding_evidence)

    async def test_limit_reply_cannot_bypass_reference_validation(self):
        context = _context()
        flow = AnswerFlow(context=context, session=None, registry={}, budget=None,
                          rounder=SimpleNamespace(budget_limited=False, advance=AsyncMock(return_value=AgentStep(
                              "[[message:999]]", (), AgentUsage()))),
                          runner=None, request_id=1)
        flow.completed_results = {"read": {"value": 1}}
        with patch.object(flow, "_run", AsyncMock(side_effect=AgentLimitReached("Limit"))), patch.object(
                flow, "_limit_reply", AsyncMock(return_value="[[message:999]]")):
            self.assertEqual(await flow.run(), GROUNDING_GAPS["source"])

    async def test_access_loss_during_repair_never_returns_source_text(self):
        from elbow_helper.agent.access import AgentAccessLost
        context = _context()
        context.state.grounding_evidence = {"read": {"messages": [MESSAGE]}}
        flow = AnswerFlow(context=context, session=None, registry={}, budget=None,
                          rounder=SimpleNamespace(advance=AsyncMock(return_value=AgentStep(
                              "Synthetic answer", (), AgentUsage()))), runner=None, request_id=1)
        with patch("elbow_helper.agent.engine.flow.require_evidence_access",
                   AsyncMock(side_effect=[None, AgentAccessLost("lost")])):
            with self.assertRaises(AgentAccessLost):
                await flow._grounded_reply("[[message:101]] [[message:999]]")
