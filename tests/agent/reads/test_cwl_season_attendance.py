"""Recent-season attendance keeps complete season units and comparable periods.

League weeks come from the family record, which holds each war once, so no
alias can double an opportunity or complete a season.
"""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from tests.temp_directory import TemporaryDirectory
import unittest
from unittest.mock import Mock

from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.cwl.config import CWL_CLAN_NAMES
from elbow_helper.features.cwl.queries import CwlQueries
from elbow_helper.agent.reads import war_tools as war_reads
from elbow_helper.agent.reads.war_tools import cwl_results
from tests.agent.engine.test_agent_plan_flow import _context
from tests.agent.reads.record_stub import with_record
from tests.features.cwl_record_stub import LeagueRecord, attack, league_war, member

MONTHS = {2: 'Q', 3: 'R', 4: 'J'}


def end_of(month, round_number, day=2):
    return int(datetime(2025, month, day, 8, tzinfo=timezone.utc).timestamp()) + round_number * 86400


class SeasonAttendanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.codes = list(CWL_CLAN_NAMES)[:2]
        self.context = await self.build()

    async def build(self, *, counts=((2, 7), (3, 7), (4, 3)), second=None, unlisted=()):
        """Each clan plays every season; #P0 misses round 1. ``second`` adds an account to the first clan."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.record = await LeagueRecord(temporary.name).initialize()
        for month, count in counts:
            for clan_index, code in enumerate(self.codes):
                wars = []
                for round_number in range(count):
                    identity = '#' + MONTHS[month] + 'PC'[clan_index] + '0289PYL'[round_number]
                    lineup = [member('#P0', name='Synthetic account', used=0 if round_number == 0 else 1)]
                    if second and clan_index == 0:
                        lineup.append(member('#P2', name='Synthetic second account', used=second(identity)))
                    if (code, identity) in unlisted:
                        lineup = []
                    attacks = [attack(index, row['tag'], '#Q0') for index, row in enumerate(lineup, 1) if row['used']]
                    wars.append(league_war(identity, round_number + 1, end_of(month, round_number), lineup, attacks,
                                           team_size=max(1, len(lineup))))
                await self.record.week(code, f'2025-{month:02}', wars)
        self.history = self.record.history()
        self.queries = CwlQueries(self.history, lambda: {})
        return replace(_context(), cwl_queries=self.queries)

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


    async def test_a_six_war_season_is_not_complete(self):
        context = await self.build(counts=((2, 7), (3, 6)))
        result = await cwl_results(context, {'metric': 'attendance', 'clan_codes': self.codes, 'history_limit': 1})
        self.assertEqual(result['component_seasons'], ['2025-02'])

    async def test_repeat_misses_stay_account_level_even_with_a_shared_member(self):
        context = await self.build(second=lambda identity: 0 if identity == '#QP0' else 1)
        links = Mock()
        links.get_links_by_tags.return_value = {tag: {'discord_user_id': 42} for tag in ('#P0', '#P2')}
        result = await cwl_results(replace(context, account_links=links),
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
        context = await self.build(unlisted={(self.codes[0], '#RP0')})
        result = await cwl_results(context, {'metric': 'attendance', 'clan_codes': self.codes, 'history_limit': 1})
        total = next(row for row in result['clan_totals'] if row['clan_code'] == self.codes[0])
        self.assertEqual(total['wars'], 7)
        self.assertFalse(total['totals_complete'])
        self.assertFalse(total['season_complete'])
        self.assertIsNone(total['missed'])
        self.assertFalse(result['coverage']['exact_ranking'])


    async def test_a_season_key_stored_with_a_suffix_stays_reachable(self):
        await self.record.week(self.codes[0], '2026-06-other', [league_war(
            '#UP0', 1, end_of(6, 0, day=20), [member('#P0', used=0)], team_size=1)])
        result = await cwl_results(self.context, {'metric': 'attendance', 'clan_codes': self.codes[:1],
                                                  'season': '2026-06-other'})
        self.assertEqual(result['war_ids'], ['CWL:#UP0'])
        self.assertEqual(result['clan_totals'][0]['missed'], 1)
        refused = await war_reads.selected_wars(with_record(self.context), {'clan_codes': self.codes[:1],
                                                                            'season': '2026-06-other'}, 'REG')
        self.assertEqual(refused['cause'], 'invalid_arguments')
        invalid = await war_reads.selected_wars(self.context, {'clan_codes': self.codes[:1], 'season': 'June'}, 'CWL')
        self.assertEqual(invalid['cause'], 'invalid_arguments')


    def test_sql_guide_and_joins_keep_both_sides_of_a_shared_war_distinct(self):
        """The old store's war tables stay readable as history."""
        from types import SimpleNamespace
        from elbow_helper.agent.reads.sql.sources import DataSource
        from elbow_helper.agent.reads.sql.guide import DataGuide
        from elbow_helper.agent.reads.sql.query import run_query
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repository = SimpleNamespace(path=Path(temporary.name) / 'wars.db')
        with closing(sqlite3.connect(repository.path)) as connection:
            connection.execute("CREATE TABLE wars(war_id TEXT,clan_code TEXT,war_type TEXT,clan_tag TEXT,"
                               "opponent_tag TEXT,state TEXT,end_ts INTEGER,last_seen_ts INTEGER,team_size INTEGER,"
                               "attacks_per_member INTEGER,cwl_season TEXT,PRIMARY KEY(war_id,clan_code))")
            connection.execute("CREATE TABLE war_roster_members(war_id TEXT,clan_code TEXT,player_tag TEXT,"
                               "player_name TEXT,attacks_expected INTEGER,attacks_used INTEGER,roster_state TEXT,"
                               "captured_ts INTEGER)")
            connection.execute("CREATE TABLE war_attacks(war_id TEXT,clan_code TEXT)")
        with closing(sqlite3.connect(repository.path)) as connection, connection:
            for code in self.codes:
                for round_number in range(7):
                    # Both clan sides deliberately share the same war tag.
                    identity = '#RP' + '0289PYL'[round_number]
                    connection.execute('''INSERT INTO wars(war_id,war_type,clan_code,clan_tag,opponent_tag,state,
                        end_ts,last_seen_ts,team_size,attacks_per_member,cwl_season) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                        (identity, 'CWL', code, CLANS[code].tag, '#Q0', 'warEnded', 1, 1, 1, 1, '2025-03'))
                    connection.execute('''INSERT INTO war_roster_members(war_id,clan_code,player_tag,player_name,
                        attacks_expected,attacks_used,roster_state,captured_ts) VALUES(?,?,?,?,?,?,?,?)''',
                        (identity, code, '#P0', 'Synthetic account', 1, 1, 'warEnded', 1))
        source = DataSource('health', repository.path, tables=tuple(
            (name, 'none', 'Synthetic war evidence') for name in ('wars', 'war_roster_members', 'war_attacks')))
        paths = SimpleNamespace(project_root=repository.path.parent, data_root=repository.path.parent)
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
