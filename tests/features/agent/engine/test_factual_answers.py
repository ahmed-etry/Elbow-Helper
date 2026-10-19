"""Numeric answers use tool numerators and denominators, without generated extras."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.engine.factual_answers import narrow_answer
from elbow_helper.features.agent.engine.grounding import GROUNDING_GAPS, GroundingError
from elbow_helper.features.agent.datasets.findings import metrics
from elbow_helper.features.agent.engine.flow import AnswerFlow
from features.agent.engine.test_agent_plan_flow import _context
from features.agent.discord_actions import test_current_channel_posts as posts
from elbow_helper.features.agent.discord_actions.messages import prepare_post


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


class FactualAnswerTests(unittest.TestCase):
    def test_count_is_only_validated_field(self):
        self.assertEqual(narrow_answer("How many members in #P0?", {"read": clan()}), "48")
        for value in (True, -1, "48", None):
            with self.subTest(value=value), self.assertRaises(GroundingError):
                narrow_answer("Member count?", {"read": clan(value)})

    def test_ambiguous_missing_and_wrong_account_counts_are_rejected(self):
        for evidence in ({}, {"one": clan(), "two": clan()},
                         {"read": {**clan(), "error": "denied"}}):
            with self.subTest(evidence=evidence), self.assertRaises(GroundingError):
                narrow_answer("Member count?", evidence)
        with self.assertRaises(GroundingError):
            narrow_answer("How many members in #P2?", {"read": clan()})

    def test_average_is_rendered_from_ratio_with_coverage(self):
        payload = sample()
        payload["aggregate"]["average_stars"]["display"] = "2.34"
        payload["aggregate"]["average_stars"]["value"] = 2.34
        answer = narrow_answer("Average stars, triple rate and average destruction for ranked defenses?",
                               {"read": payload})
        self.assertIn("2.3333", answer)
        self.assertIn("33.33%", answer)
        self.assertIn("90.00%", answer)
        self.assertIn("3 measured battles", answer)
        self.assertIn("full history is not established", answer)
        self.assertIn("Unavailable accounts: 1", answer)
        self.assertIn("2026-10-10", answer)

    def test_wrong_selection_and_raw_rows_are_not_aggregates(self):
        for question, payload in (("Average stars for ranked attacks?", sample()),
                                  ("Average stars?", {key: value for key, value in sample().items()
                                                      if key != "aggregate"})):
            with self.subTest(question=question), self.assertRaises(GroundingError):
                narrow_answer(question, {"read": payload})

    def test_missing_metric_does_not_erase_other_measured_results(self):
        for invalid in ({}, None, {"numerator": True, "denominator": 3}):
            with self.subTest(invalid=invalid):
                payload = sample()
                payload["aggregate"]["triple_rate_percent"] = invalid
                answer = narrow_answer("Average stars and triple rate for ranked defenses?", {"read": payload})
                self.assertIn("Average stars: 2.3333", answer)
                self.assertIn(GROUNDING_GAPS["figures"], answer)
                self.assertNotIn("Triple rate:", answer)

    def test_missing_results_identify_the_kind_without_a_model_rewrite(self):
        for question, kind in (("Member count?", "count"), ("Average stars?", "figures")):
            with self.subTest(question=question), self.assertRaises(GroundingError) as caught:
                narrow_answer(question, {})
            self.assertEqual(caught.exception.kind, kind)

    def test_judgement_request_retains_synthesis(self):
        self.assertIsNone(narrow_answer("Should we recruit more, given the member count?", {"read": clan()}))


class FactualFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_generated_number_and_hard_cap_never_reach_answer(self):
        context = _context()
        context.state.request_text = "How many members in #P0?"
        context.state.grounding_evidence = {"read": clan()}
        rounder = SimpleNamespace(advance=AsyncMock())
        flow = AnswerFlow(context=context, session=None, registry={}, budget=None,
                          rounder=rounder, runner=None, request_id=1)
        answer = await flow._grounded_reply("49, the hard cap. Nobody else can join.")
        self.assertEqual(answer, "48")
        rounder.advance.assert_not_awaited()

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

    async def test_immediate_reply_cannot_bypass_numeric_renderer(self):
        self.context.state.request_text = "How many members in #P0?"
        self.context.state.grounding_evidence = {"read": clan()}
        result = await prepare_post(self.context, {"channel_id": 2, "text": "49, the hard cap."})
        self.assertTrue(result["delivered_as_reply"])
        self.context.send_reply.assert_awaited_with(self.context, "48")
