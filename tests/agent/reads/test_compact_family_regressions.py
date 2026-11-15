"""Exercise retained family evidence through the production model-size boundary."""

from copy import deepcopy
import json
import unittest

from elbow_helper.agent.engine.budgets import MAX_TOOL_RESULT_CHARACTERS
from elbow_helper.agent.engine.tool_call import bound_tool_result
from elbow_helper.agent.reads import ranked_tools as ranked_reads
from elbow_helper.agent.plan.results import model_result, model_view
from tests.agent.reads.test_agent_ranked import RankedRecordFixture, iso
from tests.data.record_fixtures import BASE, SECOND, battle


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
        await self.seed_history()
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
        self.assertEqual(len([item for item in retained['rows'] if 'battle_at' in item]), 90)
        self.assertFalse(self.context.state.attachments)

    async def test_one_accounts_detailed_month_pages_through_every_battle(self):
        # One account's month of battles used to be one row larger than a page, so detail was refused.
        for index in range(90):
            self.add_battle(battle(-index - 1, attack=index % 3 != 0, stars=index % 4), key=f'key-{index}')
        await self.seed_history()
        arguments = {'account_tags': ['#P0'], 'after': iso(BASE - 3600 * SECOND), 'before': iso(BASE),
                     'detail': 'detail'}
        page = await ranked_reads.TOOLS[0].handler(self.context, arguments)
        self.assertNotIn('error', page)
        rows = list(page['rows'])
        while page.get('next_cursor'):
            self.assertLessEqual(len(json.dumps(model_view(model_result(page)), ensure_ascii=False,
                                                separators=(',', ':'))), MAX_TOOL_RESULT_CHARACTERS)
            page = await ranked_reads.TOOLS[0].handler(self.context, {
                'report_id': page['report_id'], 'cursor': page['next_cursor']})
            rows.extend(page['rows'])
        self.assertEqual(rows[0]['observed_attacks']['battles'] + rows[0]['observed_defenses']['battles'], 90)
        self.assertEqual(len(rows), 91)
        self.assertTrue(all(row['player_tag'] == '#P0' and 'battle_at' in row for row in rows[1:]))

    async def test_summary_rows_state_each_rate_once_through_both_view_passes(self):
        for index in range(6):
            self.add_battle(battle(-index - 1, stars=index % 4, destructionPercentage=50 + index), key=f'key-{index}')
        await self.seed_history()
        result = await ranked_reads.TOOLS[0].handler(self.context, {
            'account_tags': ['#P0'], 'after': iso(BASE - 3600 * SECOND), 'before': iso(BASE)})
        saved = json.loads(self.context.state.reports[result['report_id']].payload)['rows'][0]['observed_attacks']
        # A step views a family page twice before the model reads it.
        shown = unpack(delivered(delivered(result))['rows'])[0]['observed_attacks']
        self.assertEqual(shown, {
            **{key: saved[key] for key in ('battles', 'stars_total', 'triples', 'missing_stars',
                                           'missing_destruction', 'destruction_total')},
            **{rate: saved[rate]['display'] for rate in ('average_stars', 'average_destruction_percent',
                                                          'triple_rate_percent')},
            'observed_range': saved['coverage']['available_range']})

    async def test_an_account_whose_log_was_never_read_remains_unknown(self):
        result = await ranked_reads.TOOLS[0].handler(self.context, {
            'account_tags': ['#P9'], 'after': iso(BASE - 3600 * SECOND), 'before': iso(BASE)})
        row = delivered(result)['rows'][0]
        self.assertIsNone(row['observed_defenses'])
        self.assertIsNone(row['observed_attacks'])
        self.assertIsNone(row['attacks_used'])
        self.assertEqual(row['watch_status'], 'never_read')
