"""Supported numeric checks compare typed measurements without replacing prose."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.engine.factual_answers import numeric_conflicts, clan_count_tag
from elbow_helper.features.agent.datasets.findings import metrics
from elbow_helper.features.agent.engine.flow import AnswerFlow
from features.agent.engine.test_agent_plan_flow import _context
from features.agent.discord_actions import test_current_channel_posts as posts
from elbow_helper.features.agent.discord_actions.messages import prepare_post
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage, AgentToolDefinition
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.models import RegisteredAgentTool
from unittest.mock import patch
from features.agent.engine.test_agent_plan_flow import _Model, _Session, _model_step, _plan, _step


def clan(count=48):
    return {"source": "clash_api", "kind": "clan", "items": [
        {"tag": "#P0", "status": "ok", "data": {"members": count}}]}


def sample():
    values = metrics([{"stars": stars, "destructionPercentage": destruction}
                      for stars, destruction in [(3, 100), (2, 80), (2, 90)]])
    return {"source": "clash_api", "kind": "battle_log", "items": [
        {"tag": "#P0", "status": "ok"}],
        "aggregate": {**values, "selection": "ranked_defenses", "coverage": {
            "history_complete": False, "unavailable_tags": ["#P2"],
            "requested_after": "2026-10-10T00:00:00+00:00",
            "requested_before": "2026-10-15T00:00:00+00:00"}}}


class FactualFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_generated_number_gets_one_repair_with_verified_value(self):
        context = _context()
        context.state.request_text = "How many members in #P0?"
        context.state.grounding_evidence = {"read": clan()}
        rounder = SimpleNamespace(advance=AsyncMock(return_value=AgentStep(
            "There are 48 members. Recruitment can continue.", (), AgentUsage())))
        flow = AnswerFlow(context=context, session=None, registry={}, budget=None,
                          rounder=rounder, runner=None, request_id=1)
        answer = await flow._grounded_reply("49, the hard cap. Nobody else can join.")
        self.assertEqual(answer, "There are 48 members. Recruitment can continue.")
        rounder.advance.assert_awaited_once()
        self.assertIn("#P0 total members: 48", rounder.advance.await_args.kwargs["continuation_instruction"])
        self.assertEqual(rounder.advance.await_args.kwargs["reasoning_effort"], AgentReasoningEffort.LOW)

    async def test_repeated_contradiction_appends_figure_without_replacing_prose(self):
        context = _context()
        context.state.request_text = "How many members in #P0?"
        context.state.grounding_evidence = {"read": clan()}
        draft = "#P0 has 49 members. Keep recruiting."
        rounder = SimpleNamespace(advance=AsyncMock(return_value=AgentStep(draft, (), AgentUsage())))
        flow = AnswerFlow(context=context, session=None, registry={}, budget=None,
                          rounder=rounder, runner=None, request_id=1)
        answer = await flow._grounded_reply(draft)
        self.assertTrue(answer.startswith(draft))
        self.assertIn("Verified figures: #P0 total members: 48.", answer)
        rounder.advance.assert_awaited_once()

    async def test_unavailable_repair_preserves_prose_and_appends_figure(self):
        context = _context()
        context.state.request_text = "How many members in #P0?"
        context.state.grounding_evidence = {"read": clan()}
        rounder = SimpleNamespace(advance=AsyncMock(side_effect=TimeoutError))
        flow = AnswerFlow(context=context, session=None, registry={}, budget=None,
                          rounder=rounder, runner=None, request_id=1)
        answer = await flow._grounded_reply("#P0 has 49 members. Keep recruiting.")
        self.assertIn("Keep recruiting.", answer)
        self.assertIn("#P0 total members: 48.", answer)

    async def test_filtered_member_question_always_reaches_answer_round(self):
        for level in (16, 17):
            with self.subTest(level=level):
                question = f"How many members in #P0 are TH{level}?"
                self.assertIsNone(clan_count_tag(question))
                draft = f"12 members are TH{level}."
                session = _Session([_model_step(_plan([_step("read")])),
                                    AgentStep(draft, (), AgentUsage())], [])
                registry = {"read_value": RegisteredAgentTool(AgentToolDefinition(
                    "read_value", "Read synthetic evidence.", {"type": "object", "properties": {}}),
                    AsyncMock(return_value=clan()))}
                with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry):
                    answer = await AgentService(_Model(session)).answer(
                        question=question, context=_context(), local_context="")
                self.assertEqual(answer, draft)
                self.assertEqual(len(session.calls), 2)

    async def test_already_posted_answer_is_not_rendered_twice(self):
        context = _context()
        context.state.request_text = "Member count?"
        context.state.grounding_evidence = {"read": clan()}
        flow = AnswerFlow(context=context, session=None, registry={}, budget=None,
                          rounder=None, runner=None, request_id=1)
        self.assertEqual(await flow._grounded_reply(""), "")


class FactualPostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await posts.CurrentChannelPostTests.asyncSetUp(self)

    async def test_immediate_reply_preserves_authored_prose(self):
        self.context.state.request_text = "How many members in #P0?"
        self.context.state.grounding_evidence = {"read": clan()}
        result = await prepare_post(self.context, {"channel_id": 2, "text": "49, the hard cap."})
        self.assertTrue(result["delivered_as_reply"])
        self.context.send_reply.assert_awaited_with(self.context, "49, the hard cap.")


class FigureCheckTests(unittest.TestCase):
    def test_count_comparison_requires_matching_subject_and_metric(self):
        for answer in ("#P0 has 48 members.", "#P0 has 12 TH16 members.",
                       "#P0 has 12 members who missed attacks.", "#P2 has 49 members."):
            with self.subTest(answer=answer):
                self.assertFalse(numeric_conflicts("Review these clans", answer, {"read": clan()}))
        self.assertEqual(numeric_conflicts("Review #P0", "#P0 has 49 members. Keep recruiting.",
                                          {"read": clan()}), ("#P0 total members: 48.",))

    def test_ambiguous_failed_or_quoted_numeric_sources_cannot_force_a_rewrite(self):
        for evidence in ({"one": clan(), "two": clan(49)},
                         {"read": {**clan(), "error": "denied"}},
                         {"message": {"content": str(clan())}}):
            self.assertFalse(numeric_conflicts("How many members in #P0?", "49", evidence))

    def test_figures_compare_ratios_with_display_rounding_and_selection(self):
        for answer in ("Average stars: 2.33", "Average stars: 2.3333", "Triple rate: 33.33%",
                       "Ranked attack average stars: 1.5", "#P2 average stars: 1.5"):
            with self.subTest(answer=answer):
                self.assertFalse(numeric_conflicts("Average stars for ranked defenses?", answer, {"read": sample()}))
        conflicts = numeric_conflicts("Average stars for ranked defenses?",
                                      "#P0 average stars: 2.5. Keep practising.", {"read": sample()})
        self.assertEqual(len(conflicts), 1)
        self.assertIn("2.3333", conflicts[0])

    def test_figures_do_not_compare_different_requested_selections(self):
        self.assertFalse(numeric_conflicts("Average stars for ranked attacks?", "Average stars: 1.5",
                                          {"read": sample()}))

    def test_bare_figure_and_query_count_claims_are_checked(self):
        self.assertTrue(numeric_conflicts("Average stars for ranked defenses?", "1.5", {"read": sample()}))
        query = {"rows": [{"count": 2}], "row_count": 1, "truncated": False}
        self.assertEqual(numeric_conflicts("Count only", "3", {"read": query}), ("Requested count: 2.",))
        self.assertFalse(numeric_conflicts("Count only", "2", {"read": query}))
        self.assertFalse(numeric_conflicts("Count only", "3", {"read": {**query, "truncated": True}}))
