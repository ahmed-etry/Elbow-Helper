"""Recent-season attendance keeps complete season units and comparable periods."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.cwl.config import CWL_CLAN_NAMES
from elbow_helper.features.cwl.queries import CwlQueries
from elbow_helper.features.agent.families.war_reads import cwl_results
from features.agent.engine.test_agent_plan_flow import _context


class SeasonAttendanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repository = ClanHealthRepository(Path(temporary.name) / 'wars.db')
        repository.initialize()
        self.repository = repository
        self.codes = list(CWL_CLAN_NAMES)[:2]
        with closing(sqlite3.connect(repository.path)) as connection, connection:
            for month, count in ((2, 7), (3, 7), (4, 3)):
                for clan_index, code in enumerate(self.codes):
                    for round_number in range(count):
                        # Both clan sides deliberately share the same war tag.
                        identity = '#' + {2: 'Q', 3: 'R', 4: 'J'}[month] + 'P' + '0289PYL'[round_number]
                        end = month * 10000 + round_number * 100
                        connection.execute('''INSERT INTO wars(war_id,war_type,clan_code,clan_tag,
                            opponent_tag,state,preparation_start_ts,start_ts,end_ts,last_seen_ts,
                            team_size,attacks_per_member,cwl_season) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (identity, 'CWL', code, CLANS[code].tag, '#Q0', 'warEnded',
                             end - 90, end - 80, end, end, 1, 1, f'2025-{month:02}'))
                        used = 0 if round_number == 0 else 1
                        connection.execute('''INSERT INTO war_roster_members(war_id,clan_code,
                            player_tag,player_name,attacks_expected,attacks_used,roster_state,captured_ts)
                            VALUES(?,?,?,?,?,?,?,?)''',
                            (identity, code, '#P0', 'Synthetic account', 1, used, 'warEnded', end))
        self.context = replace(_context(), cwl_queries=CwlQueries(repository, lambda: {}))

    async def test_recent_seasons_include_all_wars_and_keep_per_clan_totals(self):
        result = await cwl_results(self.context, {'metric': 'attendance',
            'clan_codes': self.codes, 'history_limit': 2})
        self.assertEqual(result['component_seasons'], ['2025-03', '2025-02'])
        self.assertEqual(len(result['clan_totals']), 4)
        for row in result['clan_totals']:
            self.assertEqual((row['wars'], row['attacks_used'], row['attacks_expected'], row['missed']),
                             (7, 6, 7, 1))
            self.assertTrue(row['totals_complete'])
        self.assertEqual({row['season'] for row in result['rows']}, {'2025-02', '2025-03'})
        self.assertEqual(len(result['rows']), 4)
        self.assertEqual({(row['clan_code'], row['season']) for row in result['rows']},
                         {(code, season) for code in self.codes for season in ('2025-02', '2025-03')})
        self.assertTrue(all((row['missed'], row['attacks_used'], row['attacks_expected']) == (1, 6, 7)
                            for row in result['rows']))

    async def test_attendance_without_a_period_reads_the_latest_completed_season(self):
        result = await cwl_results(self.context, {'metric': 'attendance', 'clan_codes': self.codes})
        self.assertEqual(result['component_seasons'], ['2025-03'])
        self.assertEqual(result['period_unit'], 'season')
        self.assertTrue(all(row['wars'] == 7 for row in result['clan_totals']))

    async def test_recent_seasons_reject_an_individual_war_selector(self):
        result = await cwl_results(self.context, {'metric': 'attendance',
            'clan_codes': self.codes, 'history_limit': 2, 'latest_completed': 2})
        self.assertEqual(result['cause'], 'invalid_arguments')

    async def test_individual_war_selection_remains_a_war_period(self):
        result = await cwl_results(self.context, {'metric': 'attendance',
            'clan_codes': self.codes, 'latest_completed': 2})
        self.assertEqual(result['period_unit'], 'war')
        self.assertLess(sum(row['wars'] for row in result['clan_totals']), 7)

    async def test_multiple_seasons_use_one_reconciled_snapshot_without_roster_history_reads(self):
        with (patch.object(self.repository, 'roster_history', side_effect=AssertionError('unnecessary read')),
              patch.object(self.repository, 'attendance', wraps=self.repository.attendance) as read):
            result = await cwl_results(self.context, {'metric': 'attendance',
                'clan_codes': self.codes, 'history_limit': 2})
        self.assertEqual(read.call_count, 1)
        self.assertEqual(len(result['war_aliases']), 28)
        self.assertEqual(len(result['war_ids']), 14)
        self.assertTrue(all(row['season_complete'] for row in result['clan_totals']))

    async def test_duplicate_alias_cannot_complete_a_six_war_season(self):
        with closing(sqlite3.connect(self.repository.path)) as conn, conn:
            conn.execute("DELETE FROM wars WHERE cwl_season='2025-03' AND war_id='#RPL'")
            columns = [row[1] for row in conn.execute('PRAGMA table_info(wars)')]
            for code in self.codes:
                row = list(conn.execute("SELECT * FROM wars WHERE clan_code=? AND cwl_season='2025-03' LIMIT 1", (code,)).fetchone())
                row[columns.index('war_id')] = 'CWL:' + row[columns.index('war_id')]
                conn.execute('INSERT INTO wars VALUES (' + ','.join('?' for _ in row) + ')', row)
        result = await cwl_results(self.context, {'metric': 'attendance', 'clan_codes': self.codes, 'history_limit': 1})
        self.assertEqual(result['component_seasons'], ['2025-02'])

    async def test_aliases_reconcile_rosters_and_attacks_without_doubling_opportunities(self):
        code = self.codes[0]
        with closing(sqlite3.connect(self.repository.path)) as conn, conn:
            columns = [row[1] for row in conn.execute('PRAGMA table_info(wars)')]
            row = list(conn.execute("SELECT * FROM wars WHERE clan_code=? AND cwl_season='2025-03' ORDER BY end_ts LIMIT 1", (code,)).fetchone())
            tag = row[columns.index('war_id')]
            row[columns.index('war_id')] = 'CWL:' + tag
            conn.execute('INSERT INTO wars VALUES (' + ','.join('?' for _ in row) + ')', row)
            conn.execute('''INSERT INTO war_roster_members(war_id,clan_code,player_tag,player_name,
                attacks_expected,attacks_used,roster_state,captured_ts) VALUES(?,?,?,?,1,0,'warEnded',?)''',
                ('CWL:' + tag, code, '#P0', 'Synthetic alias', 99999))
            for identity in (tag, 'CWL:' + tag):
                conn.execute('''INSERT INTO war_attacks(war_id,war_type,clan_code,clan_tag,
                    player_tag,player_name,attack_order,defender_tag,stars,destruction,war_state,end_ts)
                    VALUES(?,'CWL',?,?,?,'Synthetic account',1,'#Q0',3,100,'warEnded',30600)''',
                    (identity, code, CLANS[code].tag, '#P0'))
        result = await cwl_results(self.context, {'metric': 'attendance', 'clan_codes': self.codes, 'history_limit': 2})
        self.assertEqual(result['duplicate_identities_removed'], 1)
        total = next(row for row in result['clan_totals'] if row['clan_code'] == code and row['season'] == '2025-03')
        self.assertEqual((total['wars'], total['attacks_used'], total['attacks_expected'], total['missed']), (7, 7, 7, 0))
        other = next(row for row in result['clan_totals'] if row['clan_code'] == self.codes[1] and row['season'] == '2025-03')
        self.assertEqual(other['missed'], 1)

    async def test_repeat_misses_stay_account_level_even_with_a_shared_member(self):
        with closing(sqlite3.connect(self.repository.path)) as conn, conn:
            conn.execute("""INSERT INTO war_roster_members
                SELECT war_id,clan_code,'#P2','Synthetic second account',townhall,map_position,
                    attacks_expected,CASE WHEN war_id='#QP0' THEN 0 ELSE 1 END,roster_state,captured_ts,source
                FROM war_roster_members WHERE clan_code=?""", (self.codes[0],))
        links = Mock()
        links.get_links_by_tags.return_value = {tag: {'discord_user_id': 42} for tag in ('#P0', '#P2')}
        result = await cwl_results(replace(self.context, account_links=links),
            {'metric': 'attendance', 'clan_codes': self.codes, 'history_limit': 2})
        missed = {}
        for row in result['rows']:
            self.assertEqual(row['unit'], 'account')
            if row['missed'] > 0:
                missed.setdefault((row['clan_code'], row['player_tag']), set()).add(row['season'])
        self.assertEqual({identity for identity, seasons in missed.items() if len(seasons) == 2},
                         {(code, '#P0') for code in self.codes})
        self.assertEqual(missed[self.codes[0], '#P2'], {'2025-02'})

    async def test_missing_final_roster_is_reported_without_complete_totals(self):
        with closing(sqlite3.connect(self.repository.path)) as conn, conn:
            conn.execute("DELETE FROM war_roster_members WHERE clan_code=? AND war_id='#RP0'", (self.codes[0],))
        result = await cwl_results(self.context, {'metric': 'attendance', 'clan_codes': self.codes, 'history_limit': 1})
        total = next(row for row in result['clan_totals'] if row['clan_code'] == self.codes[0])
        self.assertEqual(total['wars'], 7)
        self.assertFalse(total['totals_complete'])
        self.assertFalse(total['season_complete'])
        self.assertIsNone(total['missed'])
        self.assertFalse(result['coverage']['exact_ranking'])

    def test_sql_guide_and_joins_keep_both_sides_of_a_shared_war_distinct(self):
        from types import SimpleNamespace
        from elbow_helper.features.agent.datasets.sources import DataSource
        from elbow_helper.features.agent.datasets.guide import DataGuide
        from elbow_helper.features.agent.datasets.query import run_query
        source = DataSource('health', self.repository.path, tables=tuple(
            (name, 'none', 'Synthetic war evidence') for name in ('wars', 'war_roster_members', 'war_attacks')))
        paths = SimpleNamespace(project_root=self.repository.path.parent, data_root=self.repository.path.parent)
        guide = DataGuide(paths, (source,)).for_levels(set())
        self.assertIn('war_id TEXT PK1', guide)
        self.assertIn('clan_code TEXT PK2', guide)
        result, _ = run_query((source,), paths, set(), '''
            SELECT w.clan_code, count(*) AS opportunities, sum(r.attacks_expected) AS expected
            FROM health.wars w JOIN health.war_roster_members r
              ON w.war_id=r.war_id AND w.clan_code=r.clan_code
            WHERE w.cwl_season=:season GROUP BY w.clan_code ORDER BY w.clan_code
        ''', {'season': '2025-03'})
        self.assertEqual(result['rows'], [{'clan_code': code, 'opportunities': 7, 'expected': 7}
                                         for code in sorted(self.codes)])
