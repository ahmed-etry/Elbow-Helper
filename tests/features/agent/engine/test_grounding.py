"""Source integrity and useful recovery; semantic quality is evaluated with a real model."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.engine.grounding import (
    GROUNDING_GAPS, GroundingError, ground_answer, partial_answer,
)
from elbow_helper.features.agent.engine.flow import AnswerFlow
from elbow_helper.features.agent.engine.rounds import AgentLimitReached
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.capability_contract import CapabilityContract
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.features.agent.plan.results import compact_result
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage, AgentToolDefinition
from features.agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan, _step


MESSAGE = {"message_id": 101, "author_id": 202, "author": "Member Alpha",
           "content": "I am not very social, but I plan to stay.",
           "source": "https://discord.com/channels/3/91/101"}


class GroundingTests(unittest.TestCase):
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

    def test_partial_answer_keeps_actual_referenced_sources_once(self):
        answer = partial_answer(
            "Do not preserve this unverified prose. [[message:101]] [[message:999]] " + MESSAGE["source"],
            {"read": {"messages": [MESSAGE]}}, GroundingError("unknown"),
        )
        self.assertIn(MESSAGE["content"], answer)
        self.assertEqual(answer.count(MESSAGE["source"]), 1)
        self.assertIn(GROUNDING_GAPS["source"], answer)
        self.assertNotIn("unverified prose", answer)
        self.assertNotIn("999", answer)

    def test_partial_answer_does_not_dump_unreferenced_or_failed_sources(self):
        for evidence in ({"read": {"messages": [MESSAGE]}},
                         {"read": {"error": "denied", "messages": [MESSAGE]}}):
            answer = partial_answer("[[message:999]]", evidence, GroundingError("unknown"))
            self.assertEqual(answer, GROUNDING_GAPS["source"])


class GroundingFlowTests(unittest.IsolatedAsyncioTestCase):
    async def answer(self, steps, *, payload=None, context=None):
        session = _Session(steps, [])
        registry = {"read_value": RegisteredAgentTool(
            AgentToolDefinition("read_value", "Read synthetic evidence.",
                                {"type": "object", "properties": {}}),
            AsyncMock(return_value=payload or {}), contract=CapabilityContract(),
        )}
        with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry):
            answer = await AgentService(_Model(session)).answer(
                question="Would you push for feedback and why?", local_context="", context=context or _context())
        return answer, session

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
        self.assertIn(GROUNDING_GAPS["source"], answer)
        self.assertNotIn("Unverified prose", answer)

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
        from elbow_helper.features.agent.access import AgentAccessLost
        context = _context()
        context.state.grounding_evidence = {"read": {"messages": [MESSAGE]}}
        flow = AnswerFlow(context=context, session=None, registry={}, budget=None,
                          rounder=SimpleNamespace(advance=AsyncMock(return_value=AgentStep(
                              "Synthetic answer", (), AgentUsage()))), runner=None, request_id=1)
        with patch("elbow_helper.features.agent.engine.flow.require_evidence_access",
                   AsyncMock(side_effect=[None, AgentAccessLost("lost")])):
            with self.assertRaises(AgentAccessLost):
                await flow._grounded_reply("[[message:101]] [[message:999]]")
