"""Supported numeric checks compare typed measurements without replacing prose."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.engine.factual_answers import numeric_conflicts, ranked_conflicts
from elbow_helper.agent.reads.ranked_measures import metrics
from elbow_helper.agent.engine.flow import AnswerFlow
from tests.agent.engine.test_agent_plan_flow import _context
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort


def clan(count=48):
    return {"source": "clash_api", "kind": "clan", "items": [
        {"tag": "#P0", "status": "ok", "data": {"members": count}}]}


def sample():
    values = metrics([{"stars": stars, "destructionPercentage": destruction}
                      for stars, destruction in [(3, 100), (2, 80), (2, 90)]])
    selected = {'account_tags': ['#P0'], 'metric': 'average_stars', 'direction': 'defenses',
                'mode': 'ranked_and_legend', 'after': '2026-10-10T00:00:00+00:00',
                'before': '2026-10-15T00:00:00+00:00'}
    return {"source": "family_record", 'family': 'read_ranked_activity',
        'resolved_selection': selected, 'measurement': {**selected, 'field': 'aggregate.average_stars', 'places': 4},
        "aggregate": {**values, "selection": "ranked_defenses", 'direction': 'defenses',
                      'mode': 'ranked_and_legend', "coverage": {'scope': 'collected_ranked_history',
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


class FigureCheckTests(unittest.TestCase):
    def test_ranked_typed_intent_is_identical_for_paraphrases_and_non_english_requests(self):
        expected = ranked_conflicts('1.5', {'read': sample()})
        self.assertTrue(expected)
        for question in ('Average stars for ranked defenses?', 'What was their mean?',
                         'Moyenne des étoiles ?', 'ما متوسط النجوم؟', 'No topic words here'):
            self.assertEqual(numeric_conflicts(question, '1.5', {'read': sample()}, ranked_only=True), expected)

    def test_ranked_field_binding_rejects_wrong_modes_windows_metrics_and_selections(self):
        from copy import deepcopy
        original = sample()
        for field, value in [('mode', 'legend'), ('direction', 'attacks'), ('metric', 'triple_rate_percent'),
                             ('account_tags', ['#Q0']), ('after', '2020-01-01'),
                             ('field', 'rows.0.attackWins'), ('places', 2)]:
            result = deepcopy(original)
            result['measurement'][field] = value
            self.assertFalse(ranked_conflicts('1.5', {'read': result}))
        ambiguous = deepcopy(original)
        ambiguous['resolved_selection']['before'] = ambiguous['measurement']['before'] = '2026-10-16T00:00:00+00:00'
        ambiguous['aggregate']['coverage']['requested_before'] = '2026-10-16T00:00:00+00:00'
        self.assertFalse(ranked_conflicts('1.5', {'one': original, 'two': ambiguous}))
        self.assertFalse(ranked_conflicts('1.5', {'quoted': {'content': str(original)}}))

    def test_ranked_checked_ratio_and_duplicate_pages_preserve_rounding(self):
        from copy import deepcopy
        result = sample()
        self.assertTrue(ranked_conflicts('1.5', {'one': result, 'page': {**result, 'offset': 5}}))
        for metric, places, correct, wrong in [('average_stars', 4, '2.33', '2.5'),
                ('triple_rate_percent', 2, '33.33%', '50%'),
                ('average_destruction_percent', 2, '90%', '91%')]:
            measured = deepcopy(result)
            measured['resolved_selection']['metric'] = measured['measurement']['metric'] = metric
            measured['measurement'].update(field='aggregate.' + metric, places=places)
            self.assertFalse(ranked_conflicts(correct, {'read': measured}))
            self.assertTrue(ranked_conflicts(wrong, {'read': measured}))
        result['aggregate']['average_stars']['value'] = 100
        self.assertFalse(ranked_conflicts('1.5', {'read': result}))

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


    def test_bare_figure_and_query_count_claims_are_checked(self):
        self.assertTrue(numeric_conflicts("Average stars for ranked defenses?", "1.5", {"read": sample()}))
        query = {"rows": [{"count": 2}], "row_count": 1, "truncated": False}
        self.assertEqual(numeric_conflicts("Count only", "3", {"read": query}), ("Requested count: 2.",))
        self.assertFalse(numeric_conflicts("Count only", "2", {"read": query}))
        self.assertFalse(numeric_conflicts("Count only", "3", {"read": {**query, "truncated": True}}))
