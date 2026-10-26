"""Directory facts remain available without substituting parked CWL accounts."""

from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.agent.families import identity_reads, war_reads
from features.agent.engine.test_agent_plan_flow import _context
from features.agent.families.test_compact_family_regressions import delivered


class DirectoryRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_roster_summary_contains_all_nine_current_counts_and_capacity(self):
        context = _context()
        tool = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_clan_rosters')
        response = {'items': [{'tag': clan.tag, 'status': 'ok', 'data': {
            'name': 'Current ' + code, 'members': 49, 'memberList': [{'tag': '#P0'}]}}
            for code, clan in CLANS.items()]}
        with patch.object(identity_reads, 'read_clash', AsyncMock(return_value=response)):
            result = await tool.handler(context, {'basis': 'current_in_game', 'view': 'count'})
        visible = delivered(result)
        self.assertEqual(visible['rows'], [])
        self.assertEqual(len(visible['clans']), 9)
        for row in visible['clans']:
            self.assertEqual(row['name'], 'Current ' + row['clan_code'])
            self.assertEqual(row['in_game_members'], 49)
            self.assertEqual(row['in_game_capacity'], 50)
            self.assertEqual(row['open_in_game_slots'], 1)
        self.assertFalse(context.state.attachments)

    async def test_current_leagues_need_no_stored_history_and_keep_utility_failures(self):
        context = replace(_context(), cwl_queries=None)
        response = {'items': [{'tag': clan.tag, 'status': 'unavailable' if code == 'BEP' else 'ok',
            'data': {} if code == 'BEP' else {'warLeague': {'id': 1, 'name': 'Champion League III'}}}
            for code, clan in CLANS.items()]}
        tool = next(tool for tool in war_reads.TOOLS if tool.definition.name == 'read_cwl_results')
        with patch.object(war_reads, 'read_clash', AsyncMock(return_value=response)):
            result = await tool.handler(context, {'metric': 'league'})
        self.assertEqual(len(result['rows']), 9)
        utility = next(row for row in result['rows'] if row['clan_code'] == 'BEP')
        self.assertIsNone(utility['current_league'])
        self.assertEqual(utility['status'], 'unavailable')
        self.assertEqual(result['rows'][0]['current_league']['name'], 'Champion League III')
