"""Complete recruitment cohorts keep their joined attack evidence at model delivery."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.agent.families import identity_reads, workflow_reads, war_reads
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.cwl.queries import CwlQueries
from elbow_helper.features.recruitment.queries import RecruitmentQueries
from features.agent.capabilities.recruitment.test_agent_recruitment import _Channel
from features.agent.engine.test_agent_plan_flow import _context
from features.agent.families.test_compact_family_regressions import delivered, unpack


class TrialPerformanceRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_owned_accounts_share_one_war_but_keep_two_attack_opportunities(self):
        with TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / 'wars.db')
            repository.initialize()
            with closing(sqlite3.connect(repository.path)) as connection, connection:
                for kind, identity, expected in [('REG', '#R0', 2), ('CWL', '#C0', 1)]:
                    connection.execute('''INSERT INTO wars(war_id,war_type,clan_code,clan_tag,
                        opponent_tag,state,start_ts,end_ts,last_seen_ts) VALUES(?,?,?,?,?,?,?,?,?)''',
                        (identity, kind, 'BEH', CLANS['BEH'].tag, '#Y0', 'warEnded', 20, 100, 1))
                    for index, (tag, stars) in enumerate([('#P0', 3), ('#Q0', 1)], 1):
                        connection.execute('''INSERT INTO war_roster_members(war_id,clan_code,
                            player_tag,player_name,attacks_expected,attacks_used,roster_state,captured_ts)
                            VALUES(?,?,?,?,?,?,?,?)''',
                            (identity, 'BEH', tag, tag, expected, 1, 'warEnded', 1))
                        connection.execute('''INSERT INTO war_attacks(war_id,war_type,clan_code,
                            clan_tag,end_ts,war_state,player_tag,player_name,attack_order,defender_tag,stars,destruction)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (identity, kind, 'BEH', CLANS['BEH'].tag, 100, 'warEnded', tag, tag,
                             index, '#Y0', stars, 100 if stars == 3 else 40))
            context = replace(_context(), cwl_queries=CwlQueries(repository, lambda: {}),
                recruitment_queries=RecruitmentQueries(lambda: {'100': {
                    'start': '2026-09-01T00:00:00+00:00', 'days': 7, 'applicant_id': 41}},
                    clock=lambda: datetime(2026, 9, 4, tzinfo=timezone.utc)),
                account_links=SimpleNamespace(get_links_by_tags=lambda tags: {
                    tag: {'discord_user_id': 41} for tag in tags}))
            tool = next(tool for tool in workflow_reads.TOOLS if tool.definition.name == 'read_recruitment_pipeline')
            report = await tool.handler(context, {'stage': 'active_trials',
                'performance': {'war_type': 'both', 'latest_completed': 2}})
            candidate = unpack(delivered(report)['rows'])[0]
            self.assertEqual(report['rows'][0]['war_attendance']['war_ids'], ['#C0', '#R0'])
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
            repository = ClanHealthRepository(Path(directory) / 'wars.db')
            repository.initialize()
            with closing(sqlite3.connect(repository.path)) as connection, connection:
                for kind, identity in (('REG', '#R0'), ('CWL', '#C0')):
                    connection.execute('''INSERT INTO wars(war_id,war_type,clan_code,clan_tag,
                        opponent_tag,cwl_season,cwl_league,cwl_round,team_size,attacks_per_member,
                        state,preparation_start_ts,start_ts,end_ts,last_seen_ts)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (identity, kind, 'BEH', CLANS['BEH'].tag, '#Q0', '2026-09',
                         'Champion League III', 1, 15, 1, 'warEnded', 10, 20, 100, 1))
                    connection.execute('''INSERT INTO war_roster_members(war_id,clan_code,player_tag,
                        player_name,townhall,attacks_expected,attacks_used,roster_state,captured_ts)
                        VALUES(?,?,?,?,?,?,?,?,?)''',
                        (identity, 'BEH', '#P0', 'Candidate', 18, 1, 1, 'warEnded', 1))
                    connection.execute('''INSERT INTO war_attacks(war_id,war_type,clan_code,clan_tag,
                        end_ts,war_state,player_tag,player_name,attack_order,defender_tag,stars,destruction)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (identity, kind, 'BEH', CLANS['BEH'].tag, 100, 'warEnded', '#P0',
                         'Candidate', 1, '#Q0', 3, 100))
            state = {str(index + 200): {'start': '2026-09-01T00:00:00+00:00', 'days': 7,
                     'applicant_id': index + 1000} for index in range(14)}
            context = replace(_context(), cwl_queries=CwlQueries(repository, lambda: {}),
                recruitment_queries=RecruitmentQueries(lambda: state,
                    clock=lambda: datetime(2026, 9, 4, tzinfo=timezone.utc)),
                account_links=SimpleNamespace(get_links_by_tags=lambda tags: {'#P0': {'discord_user_id': 1000}}))
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
