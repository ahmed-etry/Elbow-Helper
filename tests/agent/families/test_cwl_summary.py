"""Selected arithmetic and score comparisons remain correct across read shapes."""
from dataclasses import make_dataclass, replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.families.cwl_summary import cwl_analysis
from elbow_helper.agent.families.war_reads import cwl_results
from elbow_helper.agent.families.core import family_tool
from elbow_helper.agent.families.model_view import family_model_view
from elbow_helper.agent.engine.cohort_evidence import cohort_writer_evidence
from agent.engine.test_agent_plan_flow import _context


def account(tag, *, clan='alpha', season='2025-04', profile='standard',
            attacks=1, expected=2, stars=3, score=10):
    return {'player_tag': tag, 'clan_code': clan, 'season': season,
        'profile_key': profile, 'attacks': attacks, 'attacks_expected': expected,
        'stars': stars, 'score': score, 'rank': 1}


class CwlSummaryTests(unittest.TestCase):
    def test_ratios_use_full_numerators_not_average_of_account_means(self):
        result = cwl_analysis([account('#P1'), account('#P2', attacks=5,
            expected=6, stars=9, score=6)], 'performance')
        group = result['selected_group_metrics'][0]
        self.assertEqual(group['stars_per_attack'], {'numerator': 12, 'denominator': 6, 'value': 2})
        self.assertEqual(group['participation'], {'numerator': 6, 'denominator': 8, 'value': .75})
        self.assertEqual(group['mean_observed_account_score']['value'], 8)

    def test_highest_scores_keep_ties_and_separate_periods_and_profiles(self):
        rows = [account('#P1', score=11), account('#P2', clan='beta', score=13),
            account('#P3', score=13), account('#P4', profile='other', score=30),
            account('#P5', season='2025-03', score=50),
            account('#P6', profile=None, score=99)]
        groups = cwl_analysis(rows, 'performance')['score_comparisons']
        self.assertEqual(len(groups), 3)
        group = next(g for g in groups if g['scope'] == {'season': '2025-04', 'profile_key': 'standard'})
        self.assertEqual(group['highest_score'], 13)
        self.assertEqual(group['highest_accounts'], [
            {'clan_code': 'beta', 'player_tag': '#P2'}, {'clan_code': 'alpha', 'player_tag': '#P3'}])
        self.assertEqual(group['scored_accounts'], 3)

    def test_unknowns_boolean_counts_and_no_attacks_do_not_become_zero_rates(self):
        for attacks, stars in ((None, 2), (True, 2), (0, 0), (1, None)):
            with self.subTest(attacks=attacks, stars=stars):
                result = cwl_analysis([account('#P1', attacks=attacks, stars=stars,
                    score=None, season=None)], 'performance')
                self.assertIsNone(result['selected_group_metrics'][0]['stars_per_attack']['value'])
                self.assertEqual(result['score_comparisons'], [])

    def test_ass_only_compares_the_same_defined_scoring_and_war_scope(self):
        common = {'seasons': ['2025-04'], 'completed_wars': 7,
            'profile_key': 'standard', 'difficulty_weight': .4, 'missed_mode': 'penalty'}
        scopes = [{**common, 'clan_code': code} for code in ('alpha', 'beta')]
        scopes.append({**common, 'clan_code': 'gamma', 'difficulty_weight': .8})
        rows = [{'clan_code': code, 'player_tag': '#P' + str(i), 'ass_score': value}
            for i, (code, value) in enumerate((('alpha', -3), ('beta', 7), ('gamma', 30)))]
        result = cwl_analysis(rows, 'ass', scopes=scopes)
        self.assertEqual([g['highest_score'] for g in result['score_comparisons']], [7, 30])
        single = cwl_analysis([{'player_tag': '#P1', 'ass_score': 4}], 'ass', scopes=scopes[:1])
        self.assertEqual(single['score_comparisons'][0]['highest_accounts'],
            [{'clan_code': 'alpha', 'player_tag': '#P1'}])
        self.assertEqual(cwl_analysis(rows, 'ass')['score_comparisons'], [])


class CwlReadSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_analysis_is_retained_for_authorized_writer_without_transport_header_growth(self):
        rows = [account('#P1'), account('#P2', clan='beta', score=20)]
        summary = cwl_analysis(rows, 'performance')
        source = AsyncMock(return_value={'rows': rows[:1], **summary})
        tool = family_tool('read_cwl_results', 'Read selected performance.', {}, source)
        context = _context()
        result = await tool.handler(context, {'limit': 1})
        transport = family_model_view(result)
        self.assertNotIn('selected_group_metrics', transport)
        self.assertNotIn('score_comparisons', transport)
        restored = await cohort_writer_evidence(context, {'scores': transport})
        self.assertEqual(restored['scores']['selected_group_metrics'], summary['selected_group_metrics'])
        self.assertEqual(restored['scores']['score_comparisons'], summary['score_comparisons'])
        with patch('elbow_helper.agent.engine.cohort_evidence.authorize_report',
                   AsyncMock(return_value=False)):
            denied = await cohort_writer_evidence(context, {'scores': transport})
        self.assertNotIn('score_comparisons', denied['scores'])

    async def test_read_summaries_precede_top_n_and_follow_explicit_account_selection(self):
        rows = [account('#P1', score=3), account('#P2', clan='beta', score=9)]
        Row = make_dataclass('Row', [(key, object) for key in rows[0]])
        Snapshot = make_dataclass('Snapshot', [('rows', tuple), ('seasons', tuple)])
        snapshot = Snapshot(tuple(Row(**row) for row in rows), ('2025-04',))
        context = replace(_context(), cwl_queries=SimpleNamespace(performance=Mock(return_value=snapshot)))
        for tags, expected in ((None, '#P2'), (['#P1'], '#P1')):
            with self.subTest(tags=tags), patch('elbow_helper.configuration.clans.CLAN_ORDER', ['alpha', 'beta']):
                result = await cwl_results(context, {'metric': 'performance',
                    'clan_codes': ['alpha', 'beta'], 'top_n': 1,
                    **({'account_tags': tags} if tags else {})})
            self.assertEqual(len(result['rows']), 1)
            self.assertEqual(result['score_comparisons'][0]['highest_accounts'][0]['player_tag'], expected)
            self.assertEqual(sum(g['selected_accounts'] for g in result['selected_group_metrics']),
                             1 if tags else 2)
