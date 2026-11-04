"""Complete recruitment cohorts keep their joined attack evidence at model delivery."""

from dataclasses import replace
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.families import identity_reads, workflow_reads, war_reads
from elbow_helper.features.recruitment.queries import RecruitmentQueries
from agent.capabilities.recruitment.test_agent_recruitment import _Channel
from agent.engine.test_agent_plan_flow import _context
from agent.families.record_stub import regular_id, regular_war, regular_wars_from, with_record
from agent.families.test_compact_family_regressions import delivered, unpack
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

    async def test_all_fourteen_trials_survive_with_regular_and_cwl_attack_metrics(self):
        with TemporaryDirectory() as directory:
            record = await LeagueRecord(directory).initialize()
            await record.week('BEH', '2026-09', [league_war(
                '#C0', 1, 100, [member('#P0', name='Candidate', townhall=18)], [attack(1, '#P0', '#Q0')],
                opponent='#Q0', start=20, team_size=15)], league='Champion League III')
            regular = regular_war('BEH', 100, {'#P0': (1, [3])}, opponent='#Q0', start_ts=20, attacks_per_member=1)
            state = {str(index + 200): {'start': '2026-09-01T00:00:00+00:00', 'days': 7,
                     'applicant_id': index + 1000} for index in range(14)}
            context = with_record(replace(_context(), cwl_queries=record.queries(),
                recruitment_queries=RecruitmentQueries(lambda: state,
                    clock=lambda: datetime(2026, 9, 4, tzinfo=timezone.utc)),
                account_links=SimpleNamespace(get_links_by_tags=lambda tags: {'#P0': {'discord_user_id': 1000}})))
            self.enterContext(regular_wars_from(regular))
            channels = {index + 200: _Channel(index + 200) for index in range(14)}
            for channel in channels.values():
                channel.guild = context.guild
            context.guild.get_channel_or_thread = lambda identifier: channels.get(identifier, context.source_message.channel)
            accounts = {'rows': [{'member_id': index + 1000, 'player_tag': '#P0' if index == 0 else '#Q0',
                'member_name': 'Candidate ' + str(index), 'player_name': 'Account ' + str(index),
                'nickname': 'Candidate ' + str(index), 'joined_at': '2026-08-25T12:34:56.000000+00:00',
                'trophies': 1000, 'war_stars': 1500, 'observed_at': '2026-09-04T12:34:56.000000+00:00',
                'townHallLevel': 18, 'profile_status': 'ok',
                'clan': {'tag': CLANS['BEH'].tag, 'name': 'Hellbow', 'badgeUrls': {'large': 'x' * 3000}},
                'ranked_league': {'id': 1, 'name': 'Legend', 'iconUrls': {'large': 'y' * 3000}},
                'roles': [{'role_id': value, 'name': 'Cosmetic role'} for value in range(40)]}
                for index in range(14)]}
            tool = next(tool for tool in workflow_reads.TOOLS if tool.definition.name == 'read_recruitment_pipeline')
            with patch.object(identity_reads, 'member_accounts', AsyncMock(return_value=accounts)):
                result = await tool.handler(context, {'stage': 'active_trials', 'include_accounts': True})
            visible = delivered(result)
            rows = unpack(visible['rows'])
            self.assertEqual(len(rows), 14)
            self.assertEqual({row['applicant_member_id'] for row in rows}, set(range(1000, 1014)))
            candidate = next(row for row in rows if row['applicant_member_id'] == 1000)
            self.assertEqual(candidate['war_attendance']['attacks_used'], 2)
            for kind in ('REG', 'CWL'):
                performance = candidate['attack_performance'][kind]
                self.assertEqual(performance['observed_attacks'], 1)
                self.assertEqual(performance['average_stars'], {'numerator': 3, 'denominator': 1})
            absent = next(row for row in rows if row['applicant_member_id'] == 1001)
            self.assertEqual(absent['attack_performance']['CWL']['average_stars']['denominator'], 0)
            self.assertEqual(absent['attack_performance']['CWL']['measurement_status'], 'no_attack_measurements')
            self.assertNotIn('truncated_collections', visible)
            self.assertTrue(result['performance_coverage']['coverage']['query_complete'])
            self.assertFalse(context.state.attachments)
