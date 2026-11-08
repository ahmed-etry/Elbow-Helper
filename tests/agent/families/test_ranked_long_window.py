"""Long ranked windows read the whole stored range once, with an exclusive end and stable pages."""
import unittest

from elbow_helper.agent.reads.ranked_tools import TOOLS
from agent.test_agent_ranked import RankedRecordFixture, iso
from data.record_fixtures import BASE, SECOND, battle

DAY = 86400


class RankedLongWindowTests(RankedRecordFixture, unittest.IsolatedAsyncioTestCase):
    async def test_family_counts_a_two_hundred_day_window_with_an_exclusive_end(self):
        for index, offset in enumerate((-200 * DAY, -110 * DAY, -20 * DAY, -1, 0)):
            self.add_battle(battle(offset), key=f"key-{index}")
        await self.seed_history()
        arguments = {'account_tags': ['#P0'], 'after': iso(BASE - 200 * DAY * SECOND), 'before': iso(BASE),
                     'include_battles': True}
        result = await TOOLS[0].handler(self.context, arguments)
        self.assertNotIn('error', result)
        row = result['rows'][0]
        self.assertEqual(row['attacks_used'], 4)
        self.assertEqual(len(row['battles']), 4)
        self.assertEqual(row['observed_attacks']['battles'], 4)
        self.assertFalse(row['historical_counts_complete'])
        self.assertEqual(row['watch_status'], 'never_read')  # Every battle predates the first read.
