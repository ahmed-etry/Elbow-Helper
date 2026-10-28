"""Use the real CWL facade and SQLite repository for mixed season completeness."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.families import war_reads
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.cwl.queries import CwlQueries
from agent.engine.test_agent_plan_flow import _context


class CwlCoverageRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_combined_read_retains_incomplete_clan_beside_complete_ranked_clan(self):
        with TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / 'wars.db')
            repository.initialize()
            with closing(sqlite3.connect(repository.path)) as connection, connection:
                for code, count, player in [('BEH', 7, '#P0'), ('BES', 2, '#Q0')]:
                    for index in range(count):
                        identity = '#C' + '0289PYL'[index] + ('P' if code == 'BEH' else 'Q')
                        connection.execute('''INSERT INTO wars(war_id,war_type,clan_code,clan_tag,
                            opponent_tag,cwl_season,cwl_league,cwl_round,team_size,attacks_per_member,
                            state,preparation_start_ts,start_ts,end_ts,last_seen_ts)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (identity, 'CWL', code, CLANS[code].tag, '#R0', '2026-09',
                             'Master League I', index + 1, 15, 1, 'warEnded',
                             10 + index * 100, 20 + index * 100, 100 + index * 100, 1))
                        connection.execute('''INSERT INTO war_roster_members(war_id,clan_code,player_tag,
                            player_name,townhall,attacks_expected,attacks_used,roster_state,captured_ts)
                            VALUES(?,?,?,?,?,?,?,?,?)''',
                            (identity, code, player, 'Synthetic', 17, 1, 0, 'warEnded', 1))
            queries = CwlQueries(repository, lambda: {})
            context = replace(_context(), cwl_queries=queries)
            tool = next(tool for tool in war_reads.TOOLS if tool.definition.name == 'read_cwl_results')
            complete = await tool.handler(context, {'metric': 'performance', 'season': '2026-09',
                                                   'clan_codes': ['BEH']})
            combined = await tool.handler(context, {'metric': 'performance', 'season': '2026-09',
                                                   'clan_codes': ['BEH', 'BES']})
            self.assertEqual({row['clan_code'] for row in combined['rows']}, {'BEH', 'BES'})
            self.assertEqual([row for row in combined['rows'] if row['clan_code'] == 'BEH'], complete['rows'])
            partial = next(row for row in combined['rows'] if row['clan_code'] == 'BES')
            self.assertEqual(partial['player_tag'], '#Q0')
            self.assertEqual(len(partial['war_ids']), 2)
            self.assertEqual(partial['attacks_expected'], 2)
            self.assertIsNone(partial['score'])
            self.assertIsNone(partial['rank'])
            self.assertEqual(partial['scoring_status'], 'incomplete_season')
            self.assertTrue(any(item['clan_code'] == 'BES' and item['season'] == '2026-09'
                                for item in combined['missing_sources']))
            coverage = next(item for item in combined['clan_seasons'] if item['clan_code'] == 'BES')
            self.assertEqual(coverage['wars'], 2)
            self.assertFalse(coverage['complete'])
            self.assertTrue(coverage['coverage']['query_complete'])
            seasons = await tool.handler(context, {'metric': 'performance', 'season_keys': ['2026-09'],
                                                  'clan_codes': ['BEH', 'BES']})
            self.assertEqual(seasons['rows'], combined['rows'])
            self.assertEqual(seasons['missing_sources'], combined['missing_sources'])

            # Default score scope is a season, while an explicit latest-war
            # selector must keep its narrower meaning.
            default = await tool.handler(context, {'metric': 'ass', 'clan_codes': ['BEH']})
            explicit = await tool.handler(context, {'metric': 'ass', 'clan_codes': ['BEH'],
                                                    'season': '2026-09'})
            subset = await tool.handler(context, {'metric': 'ass', 'clan_codes': ['BEH'],
                                                  'latest_completed': 1})
            self.assertEqual(default['rows'], explicit['rows'])
            self.assertEqual(default['seasons'], ['2026-09'])
            self.assertEqual(default['completed_wars'], 7)
            self.assertEqual(default['resolved_rounds'], list(range(1, 8)))
            self.assertEqual(default['rows'][0]['attacks_expected'], 7)
            self.assertEqual(subset['completed_wars'], 1)
            self.assertEqual(subset['resolved_rounds'], [7])
            self.assertEqual(subset['rows'][0]['attacks_expected'], 1)
            multi = await tool.handler(context, {'metric': 'ass', 'clan_codes': ['BEH', 'BES']})
            self.assertEqual({part['clan_code']: part['completed_wars']
                              for part in multi['clan_results']}, {'BEH': 7, 'BES': 2})

    async def test_empty_clan_does_not_borrow_another_clans_latest_season(self):
        with TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / 'wars.db')
            repository.initialize()
            with closing(sqlite3.connect(repository.path)) as connection, connection:
                connection.execute('''INSERT INTO wars(war_id,war_type,clan_code,clan_tag,
                    opponent_tag,cwl_season,state,last_seen_ts) VALUES(?,?,?,?,?,?,?,?)''',
                    ('#P0', 'CWL', 'BES', CLANS['BES'].tag, '#Q0', '2026-11', 'warEnded', 1))
                connection.execute('''INSERT INTO wars(war_id,war_type,clan_code,clan_tag,
                    opponent_tag,cwl_season,state,last_seen_ts) VALUES(?,?,?,?,?,?,?,?)''',
                    ('#P2', 'CWL', 'BEH', CLANS['BEH'].tag, '#Q0', '2026-12', 'inWar', 1))
            queries = CwlQueries(repository, lambda: {})
            self.assertIsNone(queries.latest_season(clan_code='BEH'))
            self.assertEqual(queries.latest_season(clan_code='BES'), '2026-11')
            result = await war_reads.cwl_results(replace(_context(), cwl_queries=queries),
                                                  {'metric': 'ass', 'clan_codes': ['BEH']})
            self.assertEqual(result['rows'], [])
            self.assertEqual(result['seasons'], [])
