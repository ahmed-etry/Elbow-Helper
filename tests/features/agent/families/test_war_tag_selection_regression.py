"""Canonical structured tags select the same real war and API evidence."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.agent.families.war_reads import TOOLS
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.cwl.queries import CwlQueries
from features.agent.engine.test_agent_plan_flow import _context


class WarTagSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        repository = ClanHealthRepository(Path(directory.name) / 'wars.db')
        repository.initialize()
        end = int(datetime.fromisoformat('2026-09-05T00:00:00+00:00').timestamp())
        with closing(sqlite3.connect(repository.path)) as connection, connection:
            connection.execute('''INSERT INTO wars(war_id,war_type,clan_code,clan_tag,
                opponent_tag,cwl_season,cwl_league,cwl_round,team_size,attacks_per_member,
                state,preparation_start_ts,start_ts,end_ts,last_seen_ts)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                ('#C0', 'CWL', 'BEH', CLANS['BEH'].tag, '#Q0', '2026-09',
                 'Master League I', 1, 15, 1, 'warEnded', end - 200, end - 100, end, 1))
            connection.execute('''INSERT INTO war_roster_members(war_id,clan_code,player_tag,
                player_name,townhall,attacks_expected,attacks_used,roster_state,captured_ts)
                VALUES(?,?,?,?,?,?,?,?,?)''', ('#C0', 'BEH', '#P0', 'Account', 17, 1, 1, 'warEnded', 1))
        payload = {'state': 'inWar', 'attacksPerMember': 2, 'clan': {'members': [
            {'tag': tag, 'name': tag, 'attacks': []} for tag in ('#P0', '#Q0')]}}
        self.transport = AsyncMock(return_value=SimpleNamespace(ok=True, status=200, payload=payload))
        self.context = replace(_context(), cwl_queries=CwlQueries(repository, lambda: {}),
            bot=SimpleNamespace(clash_client=SimpleNamespace(configured=True, get=self.transport)),
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
        self.assertIsNone(missing['rows'][0]['attacks_used'])

    async def test_current_war_uses_canonical_tags_and_invalid_tags_do_not_query_sources(self):
        result = await self.tools['read_current_war'].handler(self.context, {
            'clan_codes': ['BEH'], 'account_tags': ['po']})
        self.assertEqual([row['player_tag'] for row in result['rows'][0]['members']], ['#P0'])
        self.transport.reset_mock()
        with patch.object(self.context.cwl_queries, 'attendance', Mock()) as stored:
            for name, selection in [('read_current_war', {}),
                ('read_war_attendance', {'war_type': 'CWL', 'latest_completed': 1}),
                ('read_cwl_results', {'metric': 'attendance', 'latest_completed': 1})]:
                refused = await self.tools[name].handler(self.context, {**selection, 'account_tags': ['invalid!']})
                self.assertEqual(refused['cause'], 'invalid_arguments')
            stored.assert_not_called()
        self.transport.assert_not_awaited()
