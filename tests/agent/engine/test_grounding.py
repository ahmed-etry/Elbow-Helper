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
from elbow_helper.agent.engine.models import RegisteredAgentTool
from elbow_helper.agent.plan.results import compact_result
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage, AgentToolDefinition
from tests.agent.engine.test_agent_plan_flow import _context, _Model, _Session


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


    def test_binding_renders_actual_author_and_words_with_source(self):
        answer = ground_answer("[[message:101]]\nMy recommendation: leave feedback optional.",
                               {"read": {"messages": [MESSAGE]}})
        self.assertIn("Member Alpha", answer)
        self.assertIn(MESSAGE["source"], answer)
        self.assertIn("> " + MESSAGE["content"], answer)
        self.assertIn("My recommendation: leave feedback optional.", answer)


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
        from elbow_helper.agent.safety.access import AgentAccessLost
        context = _context()
        context.state.grounding_evidence = {"read": {"messages": [MESSAGE]}}
        flow = AnswerFlow(context=context, session=None, registry={}, budget=None,
                          rounder=SimpleNamespace(advance=AsyncMock(return_value=AgentStep(
                              "Synthetic answer", (), AgentUsage()))), runner=None, request_id=1)
        with patch("elbow_helper.agent.engine.flow.require_evidence_access",
                   AsyncMock(side_effect=[None, AgentAccessLost("lost")])):
            with self.assertRaises(AgentAccessLost):
                await flow._grounded_reply("[[message:101]] [[message:999]]")
