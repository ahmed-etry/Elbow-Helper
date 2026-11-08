"""Canonical structured tags select the same real war and API evidence."""
from dataclasses import replace
from datetime import datetime
import json
from tests.temp_directory import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.reads.war_tools import TOOLS
from tests.agent.engine.test_agent_plan_flow import _context
from tests.features.cwl_record_stub import LeagueRecord, league_war, member


class WarTagSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        record = await LeagueRecord(directory.name).initialize()
        end = int(datetime.fromisoformat('2026-09-05T00:00:00+00:00').timestamp())
        await record.week('BEH', '2026-09', [league_war(
            '#C0', 1, end, [member('#P0', name='Account', townhall=17)], start=end - 100, team_size=15)],
            league='Master League I')
        payload = {'state': 'inWar', 'attacksPerMember': 2, 'clan': {'members': [
            {'tag': tag, 'name': tag, 'attacks': []} for tag in ('#P0', '#Q0')]}}
        # The record's latest current-war check for the clan.
        self.current = self.enterContext(patch('elbow_helper.agent.reads.wars.current_wars', AsyncMock(
            return_value={'items': [{'tag': CLANS['BEH'].tag, 'status': 'ok', 'data': payload}]})))
        self.context = replace(_context(), cwl_queries=record.queries(),
            bot=SimpleNamespace(war_reads=SimpleNamespace()),
            account_links=SimpleNamespace(get_links_by_tags=lambda tags: {
                tag: {'discord_user_id': 41} for tag in tags}))
        self.tools = {tool.definition.name: tool for tool in TOOLS}

    async def test_attendance_and_cwl_results_normalize_before_selection_and_retention(self):
        for name, selection in [
            ('read_war_attendance', {'war_type': 'CWL', 'latest_completed': 1}),
            ('read_cwl_results', {'metric': 'attendance', 'latest_completed': 1}),
            ('read_cwl_results', {'metric': 'performance', 'season': '2026-09'}),
        ]:
            with self.subTest(name=name, selection=selection):
                canonical = await self.tools[name].handler(self.context, {
                    **selection, 'clan_codes': ['BEH'], 'account_tags': ['#P0']})
                alternate = await self.tools[name].handler(self.context, {
                    **selection, 'clan_codes': ['BEH'], 'account_tags': [' po ', '#p0']})
                self.assertNotIn('error', alternate)
                self.assertEqual(alternate['rows'], canonical['rows'])
                self.assertEqual(alternate['selection']['account_tags'], ['#P0'])
                self.assertEqual(alternate['resolved_inputs']['account_tags'], ['#P0'])
                report = self.context.state.reports[alternate['report_id']]
                self.assertEqual(json.loads(report.payload)['summary']['selection']['account_tags'], ['#P0'])
        missing = await self.tools['read_war_attendance'].handler(self.context, {
            'war_type': 'CWL', 'latest_completed': 1, 'account_tags': ['yo']})
        self.assertEqual(missing['rows'][0]['player_tag'], '#Y0')
        # Every checked war has a stored lineup, so an account in none of them is a known zero.
        self.assertEqual(missing['rows'][0]['attacks_used'], 0)
        self.assertEqual(missing['rows'][0]['participation_status'], 'not_in_checked_lineups')
