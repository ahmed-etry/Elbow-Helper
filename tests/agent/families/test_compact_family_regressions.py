"""Exercise retained family evidence through the production model-size boundary."""

from copy import deepcopy
import json
import unittest

from elbow_helper.agent.engine.budgets import MAX_TOOL_RESULT_CHARACTERS
from elbow_helper.agent.engine.tool_call import bound_tool_result
from elbow_helper.agent.reads import ranked_tools as ranked_reads
from elbow_helper.agent.plan.results import model_result, model_view
from agent.test_agent_ranked import RankedRecordFixture, iso
from data.test_ranked_import import BASE, SECOND, battle


def delivered(payload):
    view = model_view(model_result(payload))
    encoded = json.dumps(view, ensure_ascii=False, separators=(',', ':'))
    return json.loads(bound_tool_result(encoded, MAX_TOOL_RESULT_CHARACTERS))


def unpack(rows):
    return [dict(zip(rows['columns'], row)) for row in rows['rows']] if isinstance(rows, dict) else rows


class CompactRankedRegressionTests(RankedRecordFixture, unittest.IsolatedAsyncioTestCase):
    async def test_copied_and_live_legend_metrics_survive_large_battle_payloads(self):
        attacks = [dict(battle(-index - 1, mode='ranked' if index % 2 else 'legend', stars=3),
                        availableLoot=[{'name': 'Gold', 'amount': 100}] * 100) for index in range(57)]
        for index, raw in enumerate(attacks):
            self.add_battle(raw, key=f'key-{index}')
        await self.copy()
        defenses = [dict(battle(index + 1, attack=False, mode='legend', stars=2),
                         armyShareCode='payload' * 500) for index in range(33)]
        await self.live({'items': [*attacks[:2], *defenses, battle(50, mode='homeVillage')]}, 100)
        result = await ranked_reads.TOOLS[0].handler(self.context, {
            'account_tags': ['#P0'], 'after': iso(BASE - 3600 * SECOND), 'before': iso(BASE + 3600 * SECOND),
            'include_battles': True})
        original = deepcopy(result)
        visible = delivered(result)
        row = visible['rows'][0]
        self.assertEqual(row['attacks_used'], 57)
        self.assertEqual(row['observed_attacks']['battles'], 57)
        self.assertEqual(row['observed_defenses']['battles'], 33)
        self.assertEqual(row['observed_defenses']['average_stars']['denominator'], 33)
        self.assertFalse(row['historical_counts_complete'])
        self.assertEqual(visible['ranked_modes'], ['RANKED', 'LEGEND'])
        self.assertNotIn('truncated_collections', visible)
        self.assertEqual(result, original)
        retained = json.loads(self.context.state.reports[result['report_id']].payload)
        self.assertEqual(len(retained['rows'][0]['battles']), 90)
        self.assertFalse(self.context.state.attachments)

    async def test_an_account_whose_log_was_never_read_remains_unknown(self):
        result = await ranked_reads.TOOLS[0].handler(self.context, {
            'account_tags': ['#P9'], 'after': iso(BASE - 3600 * SECOND), 'before': iso(BASE)})
        row = delivered(result)['rows'][0]
        self.assertIsNone(row['observed_defenses'])
        self.assertIsNone(row['observed_attacks'])
        self.assertIsNone(row['attacks_used'])
        self.assertEqual(row['watch_status'], 'never_read')
