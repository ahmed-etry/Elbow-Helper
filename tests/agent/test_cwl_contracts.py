"""Characterize CWL facts and retained reports using synthetic feature evidence."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import sqlite3
import threading
import unittest
from unittest.mock import AsyncMock, patch

from agent.engine.test_agent_plan_flow import _context
from elbow_helper.agent.tool_support.adapters.cwl.reads import (
    read_cwl_performance, read_cwl_performance_report,
)
from elbow_helper.agent.reads.cwl import cwl_ass_scores, cwl_bonus_scores
from elbow_helper.agent.reads.war_tools import cwl_results
from elbow_helper.agent.families.identity_reads import seasonal_tags
from elbow_helper.agent.datasets.clash import read_clash
from elbow_helper.agent.read_cache import ReadCache
from elbow_helper.configuration.clans import CLANS
from elbow_helper.infrastructure.clash import ClashClient
from elbow_helper.infrastructure.ai import DeepSeekTextClient
from elbow_helper.infrastructure.persistence.revisions import release_sqlite_revision
from elbow_helper.features.cwl.bonus.analysis import BonusAnalysisService
from elbow_helper.features.cwl.queries import CwlQueries
from tests.features.test_cwl_queries import _BonusConfig, _History, _dataset
from tests.features.cwl_record_stub import LeagueRecord, attack, league_war, member


class CwlContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dataset = _dataset()
        for row in (*self.dataset['roster'], *self.dataset['attacks']):
            row['player_tag'] = '#P0'
        self.history = _History(self.dataset)
        self.forbidden = AsyncMock(side_effect=AssertionError('CWL facts must stay offline'))
        self.queries = CwlQueries(
            self.history, lambda: {}, bonus_config=_BonusConfig(),
            bonus_analysis=BonusAnalysisService(None, self.history),
            clock=lambda: datetime(2026, 9, 17, tzinfo=timezone.utc),
        )
        self.context = replace(_context(), cwl_queries=self.queries,
            bot=SimpleNamespace(clash_client=SimpleNamespace(configured=True, get=self.forbidden),
                                llm_client=SimpleNamespace(complete=self.forbidden)))
        self.enterContext(patch.object(ClashClient, 'get', self.forbidden))
        self.enterContext(patch.object(DeepSeekTextClient, 'complete', self.forbidden))

    async def asyncTearDown(self):
        self.forbidden.assert_not_called()

    async def test_rounds_one_to_seven_match_season_but_a_subset_keeps_its_sample(self):
        season = await read_cwl_performance(self.context, {'season': '2026-08', 'clan_code': 'BEH'})
        whole = await cwl_ass_scores(self.context, {'clan_code': 'BEH',
            'war_ids': [f'war-{day}' for day in range(1, 8)]})
        part = await cwl_ass_scores(self.context, {'clan_code': 'BEH', 'war_ids': ['war-2', 'war-4']})
        self.assertEqual(whole['resolved_rounds'], tuple(range(1, 8)))
        self.assertEqual(whole['players'][0]['ass_score'], season['players'][0]['score'])
        self.assertEqual((whole['completed_wars'], part['completed_wars']), (7, 2))
        self.assertEqual(part['players'][0]['projected_stars'], 21)
        self.assertEqual(len(part['attack_sample']), 2)

    async def test_bonus_uses_the_same_wars_and_retains_its_distinct_settings_and_evidence(self):
        args = {'clan_code': 'BEH', 'war_ids': ['war-2', 'war-4']}
        ass = await cwl_ass_scores(self.context, args)
        bonus = await cwl_bonus_scores(self.context, args)
        self.assertEqual(ass['resolved_war_ids'], bonus['resolved_war_ids'])
        self.assertFalse(bonus['ass_distinction']['is_ass'])
        self.assertEqual(bonus['settings']['revision'], 4)
        self.assertEqual(bonus['rows'][0]['total_adjusted_delta'], 2)
        self.assertEqual(len(bonus['attack_sample']), 2)

    async def test_saved_report_filters_without_rerunning_scoring_and_checks_guild(self):
        first = await read_cwl_performance(self.context, {'limit': 1})
        with patch.object(self.queries, 'performance', side_effect=AssertionError('retained only')):
            page = await read_cwl_performance_report(self.context,
                {'report_id': first['report_id'], 'player_tag': '#P0'})
        self.assertEqual(page['matching_rows'], 1)
        self.assertEqual(page['players'][0]['multi_season_rank'], 1)
        self.assertIn('error', await read_cwl_performance_report(
            replace(self.context, guild=SimpleNamespace(**{**vars(self.context.guild), 'id': 99})),
            {'report_id': first['report_id']}))

    async def test_season_has_no_mega_score_and_analysis_precedes_top_n(self):
        result = await cwl_results(self.context, {'metric': 'performance',
            'season': '2026-08', 'clan_codes': ['BEH'], 'top_n': 1})
        self.assertIsNone(result['rows'][0]['multi_season_score'])
        self.assertEqual(result['selected_group_metrics'][0]['stars_per_attack']['value'], 3)
        mega = await cwl_results(self.context, {'metric': 'mega_ass', 'clan_codes': ['BEH']})
        self.assertEqual(mega['rows'][0]['multi_season_rank'], 1)

    async def test_missing_setup_and_bad_score_ids_are_failures_not_empty_results(self):
        missing = replace(self.context, cwl_queries=None)
        for handler, arguments in ((read_cwl_performance, {}),
                (cwl_ass_scores, {'clan_code': 'BEH', 'war_ids': ['missing']}),
                (cwl_bonus_scores, {'clan_code': 'BEH', 'war_ids': ['missing']})):
            self.assertIn('error', await handler(missing, arguments))
        for handler in (cwl_ass_scores, cwl_bonus_scores):
            result = await handler(self.context, {'clan_code': 'BEH', 'war_ids': ['missing']})
            self.assertIn('Unknown CWL war IDs', result['error'])
        self.queries._bonus_config = None
        self.assertIn('error', await cwl_bonus_scores(self.context,
            {'clan_code': 'BEH', 'war_ids': ['war-1']}))


class CollectedCwlContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.record = await LeagueRecord(temporary.name).initialize()
        end = 1788000000
        for code, rounds, prefix in (('BEH', 7, '#CP'), ('BES', 2, '#CQ')):
            await self.record.week(code, '2026-08', [league_war(
                prefix + '0289PYL'[day], day + 1, end + 86400 * day,
                [member(tag, townhall=18, position=position) for position, tag in enumerate(('#P0', '#P2'), 1)],
                [attack(position, tag, '#Q' + '02'[position - 1], townhall=18, position=1)
                 for position, tag in enumerate(('#P0', '#P2'), 1)], team_size=15)
                for day in range(rounds)], league='Champion League II')
        await self.record.week('BEH', '2026-09', [league_war(
            '#C8', 1, end + 40 * 86400, [member('#P0')], state='inWar')], final=False)
        self.queries = self.record.queries(bonus_config=_BonusConfig(),
            bonus_analysis=BonusAnalysisService(None, self.record.history()))
        self.forbidden = AsyncMock(side_effect=AssertionError('Moved CWL handlers must stay offline'))
        self.enterContext(patch.object(ClashClient, 'get', self.forbidden))
        self.enterContext(patch.object(DeepSeekTextClient, 'complete', self.forbidden))
        self.context = replace(_context(), cwl_queries=self.queries, bot=SimpleNamespace(
            cwl_reads=self.record.reads(),
            clash_client=SimpleNamespace(configured=True, get=self.forbidden),
            llm_client=SimpleNamespace(complete=self.forbidden)))

    async def asyncTearDown(self):
        release_sqlite_revision(self.record.path)
        self.forbidden.assert_not_called()

    async def test_default_season_subset_partial_season_and_ties_keep_their_scopes(self):
        season = await cwl_results(self.context, {'metric': 'ass', 'clan_codes': ['BEH'], 'top_n': 1})
        subset = await cwl_results(self.context, {'metric': 'ass', 'clan_codes': ['BEH'], 'latest_completed': 1})
        self.assertEqual(season['completed_wars'], 7)
        self.assertEqual(season['resolved_rounds'], tuple(range(1, 8)))
        self.assertEqual(subset['completed_wars'], 1)
        self.assertEqual(len(season['score_comparisons'][0]['highest_accounts']), 2)
        self.assertEqual(season['ranked_population'], 2)
        selected = await cwl_results(self.context, {'metric': 'ass', 'clan_codes': ['BEH'],
            'account_tags': ['#P2'], 'season': '2026-08'})
        self.assertEqual(selected['rows'][0]['rank'], 1)
        partial = await cwl_results(self.context, {'metric': 'performance', 'clan_codes': ['BEH', 'BES'],
            'season': '2026-08', 'top_n': 1})
        self.assertEqual(len(partial['rows']), 1)
        self.assertEqual(sum(row['selected_accounts'] for row in partial['selected_group_metrics']), 4)
        self.assertTrue(any(row['cause'] == 'incomplete_season' for row in partial['missing_sources']))

    async def test_each_score_surface_refuses_wrong_clan_unfinished_and_unknown_ids(self):
        for key, error in (('CWL:#CQ0', 'another clan'), ('CWL:#C8', 'Unfinished'), ('#UNKNOWN', 'Unknown')):
            for metric, legacy in (('ass', cwl_ass_scores), ('bonus', cwl_bonus_scores)):
                with self.subTest(key=key, metric=metric):
                    typed = await cwl_results(self.context, {'metric': metric, 'clan_codes': ['BEH'],
                        'war_ids': [key]})
                    old = await legacy(self.context, {'clan_code': 'BEH', 'war_ids': [key]})
                    self.assertIn(error, typed['error'])
                    self.assertEqual(typed['error'], old['error'])

    async def test_io_and_scorers_run_off_the_event_loop_and_failures_remain_explicit(self):
        main_thread = threading.get_ident()
        original = self.queries._history.roster_history
        def read(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), main_thread)
            return original(*args, **kwargs)
        with patch.object(self.queries._history, 'roster_history', side_effect=read):
            self.assertNotIn('error', await cwl_results(self.context, {'metric': 'performance', 'clan_codes': ['BEH']}))
            self.assertNotIn('error', await read_cwl_performance(self.context, {'clan_code': 'BEH'}))
        with patch.object(self.queries._history, 'attendance', side_effect=sqlite3.OperationalError('locked')):
            result = await cwl_results(self.context, {'metric': 'ass', 'clan_codes': ['BEH'], 'season': '2026-08'})
        self.assertEqual(result['cause'], 'source_unavailable')

        original = self.queries._history.scoring_wars
        def score(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), main_thread)
            return original(*args, **kwargs)
        with patch.object(self.queries._history, 'scoring_wars', side_effect=score):
            for metric, legacy in (('ass', cwl_ass_scores), ('bonus', cwl_bonus_scores)):
                self.assertNotIn('error', await cwl_results(self.context,
                    {'metric': metric, 'clan_codes': ['BEH'], 'war_ids': ['CWL:#CP0']}))
                self.assertNotIn('error', await legacy(self.context,
                    {'clan_code': 'BEH', 'war_ids': ['CWL:#CP0']}))

    async def test_record_corrections_invalidate_the_shared_result_before_top_n(self):
        queries = self.record.queries()
        context = replace(self.context, cwl_queries=queries, read_cache=ReadCache())
        arguments = {'metric': 'performance', 'clan_codes': ['BEH'], 'season': '2026-08', 'top_n': 1}
        with patch.object(queries, 'performance', wraps=queries.performance) as read:
            first = await cwl_results(context, arguments)
            self.assertEqual(first, await cwl_results(context, arguments))
            read.assert_called_once()
            await self.record.execute('UPDATE war_attacks SET stars=2 WHERE attacker_member_id IN '
                '(SELECT id FROM war_members WHERE player_tag=?)', ('#P0',))
            changed = await cwl_results(context, arguments)
            self.assertEqual(read.call_count, 2)
        self.assertEqual(first['rows'][0]['player_tag'], '#P0')
        self.assertEqual(changed['rows'][0]['player_tag'], '#P2')

    async def test_family_raw_league_routes_redirect_without_network_calls(self):
        for kind, tags in (('cwl_group', ['BEH']), ('cwl_war', ['#CP0'])):
            for projection in ({}, {'detail': 'full'}, {'fields': ['clan.members']}):
                result = await read_clash(self.context, {'kind': kind, 'tags': tags, **projection})
                self.assertEqual(result['items'][0]['status'], 'record_required')
                self.assertNotIn('data', result['items'][0])

    async def test_an_unknown_war_cannot_expose_family_facts_but_external_wars_remain_readable(self):
        for own, expected in ((CLANS['BEH'].tag, 'record_required'), ('#Q8', 'ok')):
            payload = {'state': 'warEnded', 'clan': {'tag': own, 'members': []},
                       'opponent': {'tag': '#Q9', 'members': []}}
            get = AsyncMock(return_value=SimpleNamespace(ok=True, status=200, payload=payload))
            context = replace(self.context, bot=SimpleNamespace(
                cwl_reads=self.record.reads(), clash_client=SimpleNamespace(configured=True, get=get)))
            result = await read_clash(context, {'kind': 'cwl_war', 'tags': ['#C9'], 'detail': 'full'})
            self.assertEqual(result['items'][0]['status'], expected)
            self.assertEqual('data' in result['items'][0], expected == 'ok')
            get.assert_awaited_once()

    async def test_an_unreadable_war_identity_never_reopens_family_raw_facts(self):
        get = AsyncMock(return_value=SimpleNamespace(ok=True, status=200, payload={
            'state': 'inWar', 'clan': {'tag': CLANS['BEH'].tag}, 'opponent': {'tag': '#Q8'}}))
        reads = SimpleNamespace(resolve=AsyncMock(side_effect=sqlite3.OperationalError('locked')))
        context = replace(self.context, bot=SimpleNamespace(cwl_reads=reads,
            clash_client=SimpleNamespace(configured=True, get=get)))
        result = await read_clash(context, {'kind': 'cwl_war', 'tags': ['#C9']})
        self.assertEqual(result['items'][0]['status'], 'record_required')
        self.assertNotIn('data', result['items'][0])

    async def test_seasonal_cohort_falls_back_to_completed_participants_without_live_group(self):
        selected = await seasonal_tags(self.context, 'BEH', '2026-08')
        self.assertEqual(selected, (['#P0', '#P2'], '2026-08', 'stored_completed_cwl_participants'))
        with patch.object(self.queries, 'seasonal_cohort', side_effect=sqlite3.OperationalError('locked')):
            self.assertEqual(await seasonal_tags(self.context, 'BEH', '2026-08'), selected)
