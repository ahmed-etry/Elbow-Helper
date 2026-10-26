"""Family reads consume rolling source rows beyond drill-down page limits."""
from dataclasses import replace
from types import SimpleNamespace
import unittest

from elbow_helper.features.agent.families.ranked_reads import TOOLS
from features.agent.engine.test_agent_plan_flow import _context
from features.agent.test_compact_clash import read_context
from features.test_ranked_stats import battle, stamp


class RankedFullSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_only_family_keeps_all_attacks_and_defenses_past_row_fifty(self):
        rows = [battle(minute=index + 1, attack=index % 2 == 0,
                       mode='ranked' if index % 3 else 'legend', stars=3) for index in range(90)]
        api_context = read_context({'#P0': {'items': rows}})
        context = replace(_context(), bot=api_context.bot,
            account_links=SimpleNamespace(get_links_for_members=lambda members: {41: [{'player_tag': '#P0'}]}))
        result = await TOOLS[0].handler(context, {'member_ids': [41], 'after': stamp(), 'before': stamp(4)})
        self.assertEqual(result['rows'][0]['observed_attacks']['battles'], 45)
        self.assertEqual(result['rows'][0]['observed_defenses']['battles'], 45)
        self.assertEqual(result['rows'][0]['observed_attacks']['stars_total'], 135)
        context.bot.clash_client.get.assert_awaited_once()
