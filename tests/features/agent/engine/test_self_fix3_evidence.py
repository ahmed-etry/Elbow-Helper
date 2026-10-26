"""Realistic writer framing and prior spending must not trim selected members."""
import json
import unittest

from elbow_helper.features.agent.engine.answer_inputs import bounded_answer_evidence
from elbow_helper.features.agent.conversation.context import estimate_tokens
from elbow_helper.features.agent.plan.results import compact_result
from features.agent.engine.test_planner_fix2_evidence import unpack
from features.agent.engine import test_self_fix2_cohort_evidence as previous
from unittest.mock import AsyncMock


class FullWriterEvidenceTests(unittest.IsolatedAsyncioTestCase):
    flow = previous.CohortWriterTests.flow

    async def test_saved_context_and_prior_spending_keep_203_rows_and_all_accounts(self):
        source = previous.cohort()
        for i, row in enumerate(source['rows']):
            row['member_name'] = 'Member with a longer name ' + str(i)
            if i < 88:
                row['accounts'].append({'player_tag': f'#ALT{i}', 'player_name': 'Another account',
                                        'clan_code': 'BEH', 'donations': 0})
                row['account_tags'].append(f'#ALT{i}')
                row['account_names'].append('Another account')
        flow, _ = await self.flow(AsyncMock(return_value=source))
        flow.budget.spent_input = 24_082
        flow.budget.spent_output = 757
        flow.budget.spent_rounds = 2
        flow.answer_context = 'Known request context. ' * 280
        flow.context.state.grounding_evidence['local_context'] = {'content': 'Source data. ' * 210}
        self.assertTrue(await flow._prepare_answer_session(instruction='Composition constraints. ' * 35))
        prompt = flow.final_session_factory.__self__.writer['prompt']
        encoded = prompt.split('\nAvailable evidence:\n', 1)[1]
        decoded = unpack(json.loads(encoded))
        rows = decoded['results']['activity']['rows']
        self.assertEqual(len(rows), 203)
        self.assertEqual(sum(len(row['account_tags']) for row in rows), 291)
        self.assertFalse(decoded.get('truncated'))
        self.assertLessEqual(flow.budget.spent_input + flow.budget.next_input_estimate,
                             flow.budget.request_limits.input_tokens)


class EvidenceBudgetTests(unittest.TestCase):
    def test_actual_token_shortage_still_bounds_whole_records(self):
        evidence = {'activity': {'rows': compact_result([
            {'member_id': i, 'member_name': 'Member ' + str(i), 'donations': i}
            for i in range(203)])}}
        encoded = bounded_answer_evidence(evidence, 800)
        decoded = json.loads(encoded)
        self.assertTrue(decoded['truncated'])
        self.assertLessEqual(estimate_tokens(encoded), 800)
        self.assertTrue(decoded['truncated_collections'])
