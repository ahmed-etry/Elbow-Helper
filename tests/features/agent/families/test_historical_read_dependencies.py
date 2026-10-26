"""Historical CWL reads do not depend on unrelated current API data."""
from dataclasses import make_dataclass, replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.features.agent.families import war_reads
from features.agent.engine.test_agent_plan_flow import _context


class HistoricalReadDependencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_stored_performance_uses_no_current_league_lookup(self):
        Snapshot = make_dataclass('Snapshot', [('rows', tuple), ('seasons', tuple)])
        Row = make_dataclass('Row', [('player_tag', str), ('clan_code', str),
            ('season', str), ('rank', int)])
        snapshot = Snapshot((Row('#P0', 'alpha', '2025-04', 1),), ('2025-04',))
        for selection in ({}, {'season': '2025-04'}, {'history_limit': 2},
                          {'season_keys': ['2025-04']}):
            with self.subTest(selection=selection):
                performance = Mock(return_value=snapshot)
                current = AsyncMock(return_value=[])
                context = replace(_context(), cwl_queries=SimpleNamespace(performance=performance))
                with patch.object(war_reads, 'current_leagues', current), patch(
                        'elbow_helper.configuration.clans.CLAN_ORDER', ['alpha']):
                    result = await war_reads.cwl_results(context,
                        {'metric': 'performance', 'clan_codes': ['alpha'], **selection})
                self.assertNotIn('error', result)
                self.assertEqual(result['rows'][0]['player_tag'], '#P0')
                current.assert_not_awaited()

    async def test_stored_missing_setup_does_not_pay_for_a_current_profile(self):
        current = AsyncMock(return_value=[])
        with patch.object(war_reads, 'current_leagues', current):
            result = await war_reads.cwl_results(replace(_context(), cwl_queries=None),
                {'metric': 'performance', 'clan_codes': ['alpha']})
        self.assertEqual(result['cause'], 'missing_setup')
        current.assert_not_awaited()

    async def test_explicit_current_league_read_keeps_its_existing_behavior(self):
        rows = [{'clan_code': 'alpha', 'status': 'ok',
                 'current_league': {'name': 'Observed current league'}}]
        current = AsyncMock(return_value=rows)
        with patch.object(war_reads, 'current_leagues', current):
            result = await war_reads.cwl_results(_context(),
                {'metric': 'league', 'clan_codes': ['alpha']})
        self.assertEqual(result['rows'], rows)
        current.assert_awaited_once()
