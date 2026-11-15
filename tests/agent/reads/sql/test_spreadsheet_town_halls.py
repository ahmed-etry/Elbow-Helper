"""A family-account sheet joins current town halls, clans, owners and signups."""
import asyncio
from contextlib import closing
from io import BytesIO
import json
import sqlite3
from types import SimpleNamespace
import unittest

from openpyxl import load_workbook

from elbow_helper.agent.engine.catalogue import discovery_tool
from elbow_helper.agent.engine.flow import AnswerFlow
from elbow_helper.agent.engine.models import EvidenceScope
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.reads.sql.guide import DataGuide
from elbow_helper.agent.reads.sql.query import query_context, reader_levels
from elbow_helper.configuration.files import FAMILY_DATABASE
from elbow_helper.data.store.clans import collect_clans
from tests.agent.files.test_agent_spreadsheets import _context
from tests.agent.reads.dataset_helpers import seed_record_links, synthetic_datasets
from tests.data.store_fixtures import empty_store
from tests.data.test_family_store import CLANS, clan_response, player
from tests.temp_directory import TemporaryDirectory


class SpreadsheetTownHallTests(unittest.IsolatedAsyncioTestCase):
    async def test_sheet_only_opening_supplies_guide_and_prepares_joined_family_accounts(self):
        directory = self.enterContext(TemporaryDirectory())
        paths = await asyncio.to_thread(synthetic_datasets, directory, only={'rosters'})
        store = await empty_store(paths.data_root / FAMILY_DATABASE)
        run = await store.begin_run('clans', 'family_clans',
            [(kind, clan.tag) for clan in CLANS for kind in ('clan_profile', 'clan_roster')], 200)
        await store.call(collect_clans, run, CLANS,
            [clan_response(0, [player('#P0'), player('#Q0'), player('#R0')]), clan_response(1, [])], 200)
        await store.finish(run)
        await asyncio.to_thread(seed_record_links, paths, [('#P0', 11), ('#Q0', 22)])

        def profiles(connection):
            with connection:
                for tag, at, value in (('#P0', 100, '15'), ('#Q0', 100, '15'), ('#Q0', 300, '17')):
                    account = connection.execute('SELECT id FROM accounts WHERE player_tag=?', (tag,)).fetchone()[0]
                    connection.execute('INSERT INTO player_observations VALUES (?,\'townHallLevel\',?,?,NULL,?)',
                                       (account, at, value, run))
        await store.call(profiles)
        with closing(sqlite3.connect(paths.project_root / 'data' / 'rosters.sqlite3')) as connection, connection:
            connection.execute("INSERT INTO rosters(id,guild_id,name,active_cycle_id,created_ts,updated_ts) "
                               "VALUES (1,1,'CWL Signup',2,1,1)")
            connection.executemany('INSERT INTO roster_cycles(id,roster_id,cycle_key,opened_ts) VALUES (?,1,?,1)',
                                   [(1, 'previous'), (2, 'latest')])
            connection.executemany('INSERT INTO roster_members(roster_id,cycle_id,player_tag,discord_user_id,'
                                   'player_name,signed_up_ts) VALUES (1,?,?,?,?,1)',
                                   [(1, '#Q0', 22, 'Previous signup'), (2, '#P0', 11, 'Latest signup')])
        context = _context(directory)
        context.bot.paths = paths
        context.state.scope = EvidenceScope(100, frozenset(), frozenset())
        guide = await asyncio.to_thread(DataGuide, paths)
        tools, active = build_agent_tools(), {}
        opening = discovery_tool(context, tools, active, lambda: None, guide.for_levels)
        opened = await opening.handler(context, {'capabilities': ['prepare_spreadsheet'], 'intent': 'inspect'})
        self.assertEqual(list(active), ['prepare_spreadsheet'])
        self.assertEqual(opened['data_guide'], guide.for_levels(reader_levels(context)))
        self.assertIn('profiles.town_halls [none]', opened['data_guide'])
        self.assertNotIn('links.suggestions', opened['data_guide'].split('Not available to this asker:')[0])
        query_opened = await discovery_tool(context, tools, {}, lambda: None, guide.for_levels).handler(
            context, {'capabilities': ['query_bot_data'], 'intent': 'inspect'})
        self.assertEqual(opened['data_guide'], query_opened['data_guide'])
        flow = AnswerFlow(context=context, session=None, registry=active, budget=None, rounder=None,
                          runner=SimpleNamespace(planning_state=lambda: {}), request_id=None)
        self.assertEqual(json.loads(flow._answer_frame({}, allow_more=True, instruction=''))
                         ['catalogue']['data_guide'], opened['data_guide'])

        sql = """SELECT a.player_tag,t.townhall,c.family_code AS clan,m.discord_id AS owner,
            CASE WHEN s.player_tag IS NULL THEN 'not signed up' ELSE 'signed up' END AS cwl_signup
            FROM family.clan_membership cm JOIN family.clans c ON c.id=cm.clan_id AND c.is_family=1
            JOIN family.accounts a ON a.id=cm.account_id
            LEFT JOIN profiles.town_halls t ON t.account_id=a.id
            LEFT JOIN family.account_links l ON l.account_id=a.id AND l.to_at IS NULL
            LEFT JOIN family.members m ON m.id=l.member_id
            LEFT JOIN rosters.rosters r ON r.name=:signup
            LEFT JOIN rosters.roster_members s ON s.player_tag=a.player_tag
              AND s.roster_id=r.id AND s.cycle_id=r.active_cycle_id
            WHERE cm.to_at IS NULL ORDER BY a.player_tag"""
        result = await active['prepare_spreadsheet'].handler(context, {'title': 'Family accounts',
            'sheets': [{'name': 'Accounts', 'sql': sql, 'params': {'signup': 'CWL Signup'}}]})
        self.assertTrue(result['attachment_prepared'], result)
        self.assertEqual(result['rows'], 3)
        workbook = load_workbook(BytesIO(context.state.attachments[0].data))
        try:
            self.assertEqual(list(workbook['Accounts'].values), [
                ('player_tag', 'townhall', 'clan', 'owner', 'cwl_signup'),
                ('#P0', '16', 'ONE', '11', 'signed up'),
                ('#Q0', '17', 'ONE', '22', 'not signed up'),
                ('#R0', '16', 'ONE', '', 'not signed up')])
        finally:
            workbook.close()
        self.assertFalse(context.state.required_access)
        refused = await query_context(context, 'SELECT value_json FROM family.player_observations')
        self.assertIn('prohibited', refused['error'])
        columns = await query_context(context, 'SELECT * FROM profiles.town_halls LIMIT 1')
        self.assertEqual(set(columns['rows'][0]), {'account_id', 'townhall'})
