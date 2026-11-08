"""Frozen facts from the ranked surfaces before consolidation (no generated prose)."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from tests.agent.engine.test_agent_plan_flow import _context
from tests.agent.reads.test_ranked import account, stored, BATTLE, AFTER, BEFORE, DAY_TWO
from elbow_helper.agent.reads.ranked import read_ranked
from elbow_helper.agent.reads.account_activity import account_activity
from elbow_helper.agent.reads.workflow_reads import stored_ranked


def fixture():
    loss = dict(BATTLE, stars=0, destructionPercentage=20)
    legend = dict(BATTLE, battleType='legend', stars=2, destructionPercentage=80)
    defense = dict(BATTLE, attack=False, stars=1, destructionPercentage=40)
    return stored([account('#P0', battles=[BATTLE, loss, legend, defense], rank=1),
                   account('#Q0', status='never_read')])


class RankedContracts(unittest.IsolatedAsyncioTestCase):
    def context(self, source=None):
        window = AsyncMock(side_effect=lambda *args, **kwargs: deepcopy(source if source is not None else fixture()))
        forbidden = AsyncMock(side_effect=AssertionError('Ranked facts must not call a network client'))
        context = replace(_context(), bot=SimpleNamespace(ranked_reads=SimpleNamespace(window=window),
                          clash_client=SimpleNamespace(get=forbidden), llm_client=SimpleNamespace(complete=forbidden)))
        return context, window, forbidden


    async def test_detail_exact_metrics_include_losses_and_legend(self):
        context, _, forbidden = self.context()
        result = await read_ranked(context, {'member_ids': [41], 'after': AFTER, 'before': BEFORE,
                                                  'include_battles': True})
        row = result['rows'][0]
        attacks = row['observed_attacks']
        self.assertEqual((attacks['battles'], attacks['stars_total'], attacks['triples']), (3, 5, 1))
        self.assertEqual(attacks['average_stars'], {
            'numerator': 5, 'denominator': 3, 'value': 5 / 3, 'display': '1.6667'})
        self.assertEqual(attacks['average_destruction_percent']['display'], '66.67')
        self.assertEqual(attacks['triple_rate_percent']['display'], '33.33')
        self.assertEqual(row['observed_defenses']['stars_total'], 1)
        self.assertEqual(len(row['battles']), 4)
        self.assertIsNone(result['rows'][1]['observed_attacks'])
        self.assertFalse(result['source_complete'])
        forbidden.assert_not_awaited()


    async def test_hibernation_uses_stored_battle_times_and_gaps(self):
        context, _, forbidden = self.context()
        before = datetime(2026, 10, 3, tzinfo=timezone.utc)
        result = await stored_ranked(context, {'#P0': DAY_TWO / 1_000_000 - 1}, before)
        self.assertEqual(len(result['#P0']['battles']), 4)
        self.assertEqual(result['#P0']['battles'][0]['battle_at'], '2026-10-02T00:00:00+00:00')
        self.assertEqual(result['#Q0']['log_coverage']['status'], 'never_read')
        forbidden.assert_not_awaited()

    async def test_account_join_keeps_unknowns_and_exact_measures(self):
        from unittest.mock import patch
        context, _, forbidden = self.context()
        rows = [{'player_tag': '#P0'}, {'player_tag': '#Q0'}]
        with patch('elbow_helper.agent.reads.account_activity.attendance', AsyncMock(return_value={'rows': []})):
            result = await account_activity(context, rows, {'after': AFTER, 'before': BEFORE})
        self.assertEqual(rows[0]['activity']['ranked']['observed_attacks']['stars_total'], 5)
        self.assertEqual(rows[1]['activity']['ranked']['status'], 'unknown')
        self.assertFalse(result['ranked']['source_complete'])
        forbidden.assert_not_awaited()
