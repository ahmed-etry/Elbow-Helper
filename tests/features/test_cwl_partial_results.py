"""Running-season performance uses the same finished-war scores as ASS."""

import asyncio
from datetime import datetime, timezone
import sqlite3
import unittest
from unittest.mock import patch

from tests.features.cwl_record_stub import LeagueRecord, attack, league_war, member
from tests.temp_directory import TemporaryDirectory


SEASON = '2026-11'
END = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp())


class CwlPartialResultsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.record = await LeagueRecord(temporary.name).initialize()
        self.queries = self.record.queries(clock=lambda: datetime(2026, 11, 8, tzinfo=timezone.utc))

    async def week(self, code='BEH', *, season=SEASON, prefix='P', finished=5, bench=False):
        wars = []
        for index in range(7):
            lineup = [member(tag, townhall=18, position=position)
                      for position, tag in enumerate(('#P0', '#P2', '#P8'), 1)]
            if bench:
                lineup.append(member('#PY', townhall=18, position=4, used=0))
            hits = [attack(position, tag, '#Q0', stars=stars, townhall=18, position=1)
                    for position, (tag, stars) in enumerate((('#P0', 3), ('#P2', 0), ('#P8', 3)), 1)]
            state = 'warEnded' if index < finished else 'inWar' if index == finished else 'preparation'
            wars.append(league_war('#C' + prefix + '0289PYL'[index], index + 1,
                END + index * 86400, lineup, hits, state=state, team_size=15))
        await self.record.week(code, season, wars, league='Champion League II', final=finished == 7)
        return ['CWL:' + war['tag'] for war in wars[:finished]]

    async def read(self, metric='performance', *, codes=('BEH',), **selection):
        return await asyncio.to_thread(self.queries.results, metric=metric, clan_codes=list(codes), **selection)

    async def test_finished_war_scores_and_tied_ranks_match_ass_before_filtering(self):
        war_ids = await self.week()
        ass = await self.read('ass', war_ids=war_ids)
        result = await self.read(season=SEASON)
        expected = {row['player_tag']: (row['ass_score'], row['rank']) for row in ass['rows']}
        self.assertEqual({row['player_tag']: (row['score'], row['rank']) for row in result['rows']}, expected)
        self.assertTrue(all(row['score'] is not None for row in result['rows']))
        self.assertEqual(expected['#P0'][1], expected['#P8'][1])
        self.assertTrue(all(row['scoring_status'] == 'finished_wars_so_far' for row in result['rows']))
        self.assertTrue(all(set(row['war_ids']) == set(war_ids) for row in result['rows']))
        self.assertEqual(result['missing_sources'], [])
        summary = result['clan_seasons'][0]
        self.assertEqual((summary['complete'], summary['wars'], summary['accounts']), (False, 5, 3))
        self.assertEqual(summary['scoring_status'], 'finished_wars_so_far')
        selected = await self.read(season=SEASON, account_tags=['#P2'], top_n=1)
        self.assertEqual((selected['rows'][0]['score'], selected['rows'][0]['rank']), expected['#P2'])
        self.assertEqual(selected['ranked_population'], 3)

    async def test_accounts_without_attacks_stay_unscored_and_keep_the_clan_gap(self):
        await self.week(bench=True)
        result = await self.read(season=SEASON)
        unused = next(row for row in result['rows'] if row['player_tag'] == '#PY')
        self.assertIsNone(unused['score'])
        self.assertIsNone(unused['rank'])
        self.assertEqual(unused['scoring_status'], 'finished_wars_so_far')
        self.assertEqual(result['missing_sources'], [{'source': 'CWL performance', 'clan_code': 'BEH',
            'season': SEASON, 'cause': 'incomplete_season'}])
        # Filtering out the unscored account must not erase the clan's gap.
        selected = await self.read(season=SEASON, account_tags=['#P0'])
        self.assertEqual(selected['missing_sources'], result['missing_sources'])

    async def test_no_finished_wars_preserves_missing_history_without_scoring(self):
        await self.week(finished=0)
        with patch.object(self.queries, 'ass_wars', side_effect=AssertionError('No finished wars')):
            result = await self.read(season=SEASON)
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['missing_sources'], [{'source': 'CWL performance', 'clan_code': 'BEH',
            'season': SEASON, 'cause': 'missing_history'}])
        summary = result['clan_seasons'][0]
        self.assertEqual((summary['complete'], summary['wars'], summary['accounts']), (False, 0, 0))
        self.assertNotIn('scoring_status', summary)

    async def test_complete_and_running_clans_keep_separate_scores_and_coverage(self):
        await self.week(finished=7)
        war_ids = await self.week('BES', prefix='Q')
        complete = await self.read(season=SEASON)
        ass = await self.read('ass', codes=('BES',), war_ids=war_ids)
        combined = await self.read(codes=('BEH', 'BES'), season=SEASON)
        self.assertEqual([row for row in combined['rows'] if row['clan_code'] == 'BEH'], complete['rows'])
        self.assertEqual({row['player_tag']: (row['score'], row['rank']) for row in combined['rows']
                          if row['clan_code'] == 'BES'},
                         {row['player_tag']: (row['ass_score'], row['rank']) for row in ass['rows']})
        self.assertEqual({row['clan_code']: (row['complete'], row['wars'])
                          for row in combined['clan_seasons']}, {'BEH': (True, 7), 'BES': (False, 5)})
        self.assertEqual(combined['missing_sources'], [])

    async def test_history_and_mega_ass_exclude_the_running_season(self):
        await self.week(season='2026-10', finished=7)
        previous = {metric: await self.read(metric, history_limit=3) for metric in ('performance', 'mega_ass')}
        await self.week(prefix='Q')
        with patch.object(self.queries, 'ass_wars', side_effect=AssertionError('Complete seasons only')):
            for metric, before in previous.items():
                with self.subTest(metric=metric):
                    after = await self.read(metric, history_limit=3)
                    self.assertEqual(after, before)
                    self.assertEqual(after['seasons'], ('2026-10',))
            explicit = await self.read('mega_ass', season=SEASON)
        self.assertTrue(all(row['score'] is None and row.get('multi_season_score') is None
                            for row in explicit['rows']))

    async def test_partial_scoring_failures_remain_explicit(self):
        await self.week()
        for failure in (sqlite3.OperationalError('locked'), RuntimeError('Scoring unavailable')):
            with self.subTest(failure=failure), patch.object(self.queries, 'ass_wars', side_effect=failure):
                result = await self.read(season=SEASON)
            self.assertEqual(result, {'error': str(failure), 'cause': 'source_unavailable'})

    async def test_missing_attack_details_leave_running_season_unscored(self):
        await self.week()
        with patch.object(self.queries, 'ass_wars', side_effect=ValueError('Incomplete scoring evidence')):
            result = await self.read(season=SEASON)
        self.assertTrue(result['rows'])
        self.assertTrue(all(row['score'] is None and row['scoring_status'] == 'incomplete_season'
                            for row in result['rows']))
        self.assertEqual([row['cause'] for row in result['missing_sources']], ['incomplete_season'])
