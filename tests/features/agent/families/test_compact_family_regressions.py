"""Exercise retained family evidence through the production model-size boundary."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.datasets import ranked
from elbow_helper.features.agent.engine.budgets import MAX_TOOL_RESULT_CHARACTERS
from elbow_helper.features.agent.engine.tool_call import bound_tool_result
from elbow_helper.features.agent.families import ranked_reads
from elbow_helper.features.agent.plan.results import model_result, model_view
from elbow_helper.features.ranked_stats.queries import RankedQueries
from elbow_helper.features.ranked_stats.store import RankedStore
from features.test_ranked_stats import battle, observation, save_log, stamp
from features.agent.engine.test_agent_plan_flow import _context


def delivered(payload):
    view = model_view(model_result(payload))
    encoded = json.dumps(view, ensure_ascii=False, separators=(',', ':'))
    return json.loads(bound_tool_result(encoded, MAX_TOOL_RESULT_CHARACTERS))


def unpack(rows):
    return [dict(zip(rows['columns'], row)) for row in rows['rows']] if isinstance(rows, dict) else rows


class CompactRankedRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stored_ranked_and_live_legend_metrics_survive_large_battle_payloads(self):
        with TemporaryDirectory() as directory:
            store = RankedStore(Path(directory) / 'ranked.db')
            store.initialize()
            attacks = [dict(battle(minute=index + 1, mode='ranked' if index % 2 else 'legend', stars=3),
                            availableLoot=[{'name': 'Gold', 'amount': 100}] * 100) for index in range(57)]
            save_log(store, attacks, observation(member=41))
            defenses = [dict(battle(day=2, minute=index, attack=False, mode='legend', stars=2),
                             armyShareCode='payload' * 500) for index in range(33)]
            live = {'items': [{'tag': '#P0', 'status': 'ok', 'data': {'items': [*attacks[-2:], *defenses,
                battle(day=2, minute=50, mode='HOME_VILLAGE')]}}]}
            context = replace(_context(), ranked_queries=RankedQueries(store))
            tool = ranked_reads.TOOLS[0]
            with patch.object(ranked, '_live_history', AsyncMock(return_value=live)):
                result = await tool.handler(context, {'account_tags': ['#P0'], 'after': stamp(),
                    'before': stamp(4), 'include_battles': True})
            original = deepcopy(result)
            visible = delivered(result)
            row = visible['rows'][0]
            self.assertEqual(row['attacks_used'], 57)
            self.assertEqual(row['observed_attacks']['battles'], 57)
            self.assertEqual(row['observed_defenses']['battles'], 33)
            self.assertEqual(row['observed_defenses']['average_stars']['denominator'], 33)
            self.assertEqual(row['duplicate_copies_removed'], 2)
            self.assertFalse(row['historical_counts_complete'])
            self.assertEqual(visible['ranked_modes'], ['RANKED', 'LEGEND'])
            self.assertNotIn('truncated_collections', visible)
            self.assertEqual(result, original)
            retained = json.loads(context.state.reports[result['report_id']].payload)
            self.assertEqual(len(retained['rows'][0]['battles']), 90)
            self.assertFalse(context.state.attachments)

    async def test_missing_live_and_stored_evidence_remains_unknown(self):
        context = replace(_context(), ranked_queries=None)
        with patch.object(ranked, '_live_history', AsyncMock(return_value={'items': [
                {'tag': '#P0', 'status': 'unavailable'}]})):
            result = await ranked_reads.TOOLS[0].handler(context, {'account_tags': ['#P0'],
                'after': stamp(), 'before': stamp(4)})
        row = delivered(result)['rows'][0]
        self.assertIsNone(row['observed_defenses'])
        self.assertIsNone(row['observed_attacks'])
        self.assertEqual(row['live_status'], 'unavailable')
