"""Long ranked reads aggregate disjoint stored windows and retain coverage gaps."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.datasets import ranked
from elbow_helper.agent.families.ranked_reads import TOOLS
from elbow_helper.features.ranked_stats.queries import RankedQueries
from elbow_helper.features.ranked_stats.store import RankedStore
from agent.engine.test_agent_plan_flow import _context
from features.test_ranked_stats import observation, battle, save_log


class RankedLongWindowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = RankedStore(Path(temp.name) / 'ranked.sqlite3')
        self.store.initialize()
        self.context = replace(_context(), ranked_queries=RankedQueries(self.store))
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.end = self.start + timedelta(days=200)
        self.args = {'member_ids': [42], 'after': self.start.isoformat(), 'before': self.end.isoformat()}

    def seed(self, tag, day, trophies):
        stamp = (self.start + timedelta(days=day)).isoformat(timespec='microseconds')
        row = {**observation(tag, trophies=trophies), 'observed_at': stamp}
        entry = {**battle(), 'battleTimestamp': stamp}
        save_log(self.store, [entry], row)

    async def test_family_reads_boundaries_once_and_merges_accounts_before_ranking_and_paging(self):
        for day in (0, 90, 180, 199, 200):
            self.seed('#P0', day, day)
        self.seed('#P2', 10, 300)
        with patch.object(self.store, 'read_window', wraps=self.store.read_window) as windows, \
                patch.object(ranked, '_live_history', AsyncMock(return_value={'items': []})):
            result = await TOOLS[0].handler(self.context, {**self.args, 'include_battles': True})
        self.assertNotIn('error', result)
        self.assertEqual(windows.call_count, 3)
        ranges = [(datetime.fromisoformat(call.kwargs['after']), datetime.fromisoformat(call.kwargs['before']))
                  for call in windows.call_args_list]
        self.assertEqual(ranges[0][0], self.start)
        self.assertEqual(ranges[-1][1], self.end)
        self.assertTrue(all(end - start <= timedelta(days=90) for start, end in ranges))
        self.assertEqual([end for _, end in ranges[:-1]], [start for start, _ in ranges[1:]])
        rows = {row['player_tag']: row for row in result['rows']}
        self.assertEqual(rows['#P0']['attacks_used'], 4)
        self.assertEqual(rows['#P0']['sample_count'], 4)
        self.assertEqual(len(rows['#P0']['battles']), 4)
        self.assertEqual(rows['#P0']['ranking']['trophies'], 199)
        self.assertEqual(rows['#P0']['selection_rank'], 2)
        self.assertEqual(rows['#P2']['selection_rank'], 1)
        self.assertFalse(rows['#P0']['historical_counts_complete'])
        with patch.object(ranked, '_live_history', AsyncMock(return_value={'items': []})):
            page = await ranked.read_ranked_stats(self.context, {**self.args, 'offset': 1, 'limit': 1})
        self.assertEqual(page['total_accounts'], 2)
        self.assertEqual(page['accounts'][0]['player_tag'], '#P0')
        self.assertIsNone(page['next_offset'])

    async def test_later_window_failure_is_not_returned_as_complete_history(self):
        with patch.object(self.store, 'read_window', side_effect=[([], False), sqlite3.OperationalError('synthetic')]), \
                patch.object(ranked, '_live_history', AsyncMock()) as live:
            result = await ranked.read_ranked_stats(self.context, self.args)
        self.assertEqual(result, {'error': 'Ranked observations could not be read.'})
        live.assert_not_awaited()

    async def test_source_limits_and_empty_history_stay_explicit_for_long_windows(self):
        with patch.object(self.store, 'read_window', side_effect=[([], False), ([], True), ([], False)]), \
                patch.object(ranked, '_live_history', AsyncMock(return_value={'items': []})):
            result = await ranked.read_ranked_stats(self.context, self.args)
        self.assertFalse(result['complete'])
        self.assertIn('snapshot_row_limit_reached', result['limitations'])
        self.assertEqual(result['history_coverage']['status'], 'empty')
        self.assertEqual(result['unobserved_selections'], [42])
        self.assertEqual(result['history_coverage']['requested_after'], self.args['after'])

