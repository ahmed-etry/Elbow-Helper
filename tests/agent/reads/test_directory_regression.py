"""Directory facts remain available without substituting parked CWL accounts."""

from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.reads import war_tools as war_reads
from tests.agent.request_fixtures import flow_context as _context

class DirectoryRegressionTests(unittest.IsolatedAsyncioTestCase):
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
