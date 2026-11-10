"""Use the real CWL facade over league weeks in the family record for mixed season completeness."""

import asyncio
from dataclasses import dataclass, replace
from tests.temp_directory import TemporaryDirectory
import unittest
from unittest.mock import Mock

from elbow_helper.agent.reads import war_tools as war_reads
from elbow_helper.features.cwl.results import CwlResults, PERIOD_SETTINGS
from tests.agent.engine.test_agent_plan_flow import _context
from tests.features.cwl_record_stub import LeagueRecord, league_war, member

END = 1788000000


class CwlCoverageRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_all_period_settings_share_validation_selection_and_defaulting(self):
        @dataclass
        class Snapshot:
            rows: tuple = ()
            summaries: tuple = ()

        periods = ({'season': '2026-11'}, {'season_keys': ['2026-11']},
                   {'war_ids': ['synthetic']}, {'latest_completed': 2}, {'history_limit': 2},
                   {'after': '2026-11-01T00:00:00+00:00', 'before': '2026-11-08T00:00:00+00:00'})
        self.assertEqual(set(PERIOD_SETTINGS), {key for period in periods for key in period})
        for metric in ('ass', 'bonus'):
            for period in periods:
                with self.subTest(metric=metric, period=period):
                    queries = Mock()
                    queries.latest_season.side_effect = AssertionError('An explicit period must not default.')
                    queries.attendance.return_value = {'war_ids': [], 'rows': [], 'coverage': 'Synthetic period'}
                    queries.ass_wars.return_value = queries.bonus_wars.return_value = Snapshot()
                    results = CwlResults(queries)
                    arguments = {'metric': metric, 'clan_codes': ['BEH'], **period}
                    self.assertIsNone(results.validate_selection(arguments))
                    self.assertNotIn('error', results.selection(arguments))
                    self.assertNotIn('error', results.read(arguments))
                    queries.latest_season.assert_not_called()
                    conflicting = {'latest_completed': 1} if 'season' in period else {'season': '2026-10'}
                    self.assertEqual(results.validate_selection({**arguments, **conflicting})['cause'],
                                     'invalid_arguments')

    async def test_combined_read_retains_incomplete_clan_beside_complete_ranked_clan(self):
        with TemporaryDirectory() as directory:
            record = await LeagueRecord(directory).initialize()
            for code, count, player in [('BEH', 7, '#P0'), ('BES', 2, '#Q0')]:
                await record.week(code, '2026-09', [
                    league_war('#C' + '0289PYL'[index] + ('P' if code == 'BEH' else 'Q'), index + 1,
                               END + index * 86400, [member(player, name='Synthetic', townhall=17, used=0)],
                               opponent='#R0', team_size=15)
                    for index in range(count)], league='Master League I')
            queries = record.queries()
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
            self.assertEqual(partial['attacks_expected'], 0)
            self.assertEqual(partial['unconfirmed_opportunities'], 2)
            self.assertFalse(partial['totals_complete'])
            self.assertIsNone(partial['score'])
            self.assertIsNone(partial['rank'])
            self.assertEqual(partial['scoring_status'], 'finished_wars_so_far')
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
            record = await LeagueRecord(directory).initialize()
            await record.week('BES', '2026-11', [league_war('#P0', 1, END, [member('#C0')])])
            await record.week('BEH', '2026-12', [league_war('#P2', 1, END + 30 * 86400, [member('#C2')],
                                                            state='inWar')], final=False)
            queries = record.queries()
            self.assertIsNone(await asyncio_thread(queries.latest_season, clan_code='BEH'))
            self.assertEqual(await asyncio_thread(queries.latest_season, clan_code='BES'), '2026-11')
            result = await war_reads.cwl_results(replace(_context(), cwl_queries=queries),
                                                  {'metric': 'ass', 'clan_codes': ['BEH']})
            self.assertEqual(result['rows'], [])
            self.assertEqual(result['seasons'], [])


async def asyncio_thread(method, **kwargs):
    return await asyncio.to_thread(method, **kwargs)
