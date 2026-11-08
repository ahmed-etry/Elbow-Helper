"""Complete recruitment cohorts keep their joined attack evidence at model delivery."""

from dataclasses import replace
from datetime import datetime, timezone
from tests.temp_directory import TemporaryDirectory
from types import SimpleNamespace
import unittest

from elbow_helper.agent.reads import workflow_reads
from elbow_helper.agent.reads import war_tools as war_reads
from elbow_helper.features.recruitment.queries import RecruitmentQueries
from tests.agent.engine.test_agent_plan_flow import _context
from tests.agent.reads.record_stub import regular_id, regular_war, regular_wars_from, with_record
from tests.agent.reads.test_compact_family_regressions import delivered, unpack
from tests.features.cwl_record_stub import LeagueRecord, attack, league_war, member


class TrialPerformanceRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_owned_accounts_share_one_war_but_keep_two_attack_opportunities(self):
        with TemporaryDirectory() as directory:
            record = await LeagueRecord(directory).initialize()
            await record.week('BEH', '1970-01', [league_war(
                '#C0', 1, 100, [member(tag, name=tag) for tag in ('#P0', '#Q0')],
                [attack(index, tag, '#Y0', stars=stars, destruction=100 if stars == 3 else 40, fresh=index == 1)
                 for index, (tag, stars) in enumerate([('#P0', 3), ('#Q0', 1)], 1)], opponent='#Y0', start=20)])
            regular = regular_war('BEH', 100, {'#P0': (1, [3]), '#Q0': (1, [1])}, opponent='#Y0', start_ts=20)
            context = with_record(replace(_context(), cwl_queries=record.queries(),
                recruitment_queries=RecruitmentQueries(lambda: {'100': {
                    'start': '2026-09-01T00:00:00+00:00', 'days': 7, 'applicant_id': 41}},
                    clock=lambda: datetime(2026, 9, 4, tzinfo=timezone.utc)),
                account_links=SimpleNamespace(get_links_by_tags=lambda tags: {
                    tag: {'discord_user_id': 41} for tag in tags})))
            self.enterContext(regular_wars_from(regular))
            tool = next(tool for tool in workflow_reads.TOOLS if tool.definition.name == 'read_recruitment_pipeline')
            report = await tool.handler(context, {'stage': 'active_trials',
                'performance': {'war_type': 'both', 'latest_completed': 2}})
            candidate = unpack(delivered(report)['rows'])[0]
            self.assertEqual(report['rows'][0]['war_attendance']['war_ids'], ['CWL:#C0', regular_id(regular)])
            self.assertEqual(candidate['war_attendance']['attacks_expected'], 6)
            for kind in ('REG', 'CWL'):
                metrics = candidate['attack_performance'][kind]
                self.assertEqual((metrics['wars'], metrics['account_war_opportunities']), (1, 2))
                self.assertEqual(metrics['average_stars'], {'numerator': 4, 'denominator': 2})
                self.assertEqual(metrics['triple_rate_percent'], {'numerator': 100, 'denominator': 2})
            attendance_tool = next(tool for tool in war_reads.TOOLS if tool.definition.name == 'read_war_attendance')
            selection = {'war_type': 'both', 'latest_completed': 2, 'account_tags': ['#P0', '#Q0']}
            members = await attendance_tool.handler(context, {**selection, 'unit': 'member'})
            metrics = members['rows'][0]['attack_performance']
            self.assertEqual((metrics['wars'], metrics['account_war_opportunities']), (2, 4))
            self.assertEqual((metrics['average_stars']['numerator'], metrics['average_stars']['denominator']), (8, 4))
            accounts = await attendance_tool.handler(context, selection)
            by_tag = {row['player_tag']: row for row in accounts['rows']}
            self.assertEqual(by_tag['#P0']['attacks_expected'], 3)
            self.assertEqual(by_tag['#Q0']['attacks_expected'], 3)
            for tag, numerator in [('#P0', 6), ('#Q0', 2)]:
                average = by_tag[tag]['attack_performance']['average_stars']
                self.assertEqual((average['numerator'], average['denominator']), (numerator, 2))
