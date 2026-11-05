"""Family reads consume every stored battle, beyond drill-down page limits."""
import unittest

from elbow_helper.agent.families.ranked_reads import TOOLS
from agent.test_agent_ranked import RankedRecordFixture, iso
from data.test_ranked_import import BASE, SECOND, battle


class RankedFullSourceTests(RankedRecordFixture, unittest.IsolatedAsyncioTestCase):
    async def test_family_keeps_all_attacks_and_defenses_past_row_fifty(self):
        rows = [battle(index + 1, attack=index % 2 == 0, mode='ranked' if index % 3 else 'legend', stars=3)
                for index in range(90)]
        await self.live({'items': rows}, 100)
        result = await TOOLS[0].handler(self.context, {'member_ids': [100], 'after': iso(BASE),
                                                       'before': iso(BASE + 200 * SECOND)})
        self.assertEqual(result['rows'][0]['observed_attacks']['battles'], 45)
        self.assertEqual(result['rows'][0]['observed_defenses']['battles'], 45)
        self.assertEqual(result['rows'][0]['observed_attacks']['stars_total'], 135)
