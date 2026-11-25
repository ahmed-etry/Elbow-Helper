"""Hibernation joins real completed-war evidence without inventing attack dates."""
from dataclasses import replace
from datetime import datetime, timezone
from tests.temp_directory import TemporaryDirectory
from types import SimpleNamespace
import unittest

from elbow_helper.configuration.channels import OVERSEEING_TERRACE
from elbow_helper.agent.reads.workflow_reads import TOOLS
from elbow_helper.features.hibernation.queries import HibernationQueries
from tests.agent.request_fixtures import flow_context as _context
from tests.agent.reads.record_stub import regular_war, regular_wars_from, with_record
from tests.features.cwl_record_stub import LeagueRecord, attack, league_war, member


class HibernationWarTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_overlapping_wars_keep_unknown_attack_counts_and_explicit_war_end_dates(self):
        with TemporaryDirectory() as directory:
            regular = [regular_war('BEH', end, {tag: (1, []) for tag in (('#P0', '#Q0') if start == 1000 else ('#P0',))},
                                   start_ts=start)
                       for start, end in [(900, 1100), (1000, 1200), (None, 1300), (800, 999)]]
            record = await LeagueRecord(directory).initialize()
            await record.week('BEH', '1970-01', [
                league_war(identity, round_no, end, [member(tag, name=tag, expected=2, used=1) for tag in
                                                     (('#P0', '#Q0') if start == 1000 else ('#P0',))],
                           [attack(order, tag, '#Q8') for order, tag in enumerate(
                               (('#P0', '#Q0') if start == 1000 else ('#P0',)), 1)],
                           opponent='#Q2', start=start, attacks_per_member=2)
                for round_no, identity, start, end in [(1, '#R0', 900, 1100), (2, '#R2', 1000, 1200)]])
            context = _context()
            for role in context.member.roles:
                role.name = 'Access'
                role.is_default = lambda: False
            log = SimpleNamespace(**{**vars(context.source_message.channel), 'id': OVERSEEING_TERRACE})
            context.guild.get_channel_or_thread = lambda identifier: log if identifier == OVERSEEING_TERRACE else context.source_message.channel
            context = with_record(replace(context, cwl_queries=record.queries(),
                hibernation_queries=HibernationQueries(lambda: {'41': {'hibernation_date': '<t:1000:F>'}}),
                account_links=SimpleNamespace(get_links_for_members=lambda ids: {
                    41: [{'player_tag': '#P0'}, {'player_tag': '#Q0'}]})))
            tool = next(tool for tool in TOOLS if tool.definition.name == 'read_hibernation')
            with regular_wars_from(*regular):
                result = await tool.handler(context, {'include_activity': True,
                    'activity_kinds': ['regular_war', 'cwl']})
            self.assertNotIn('error', result)
            by_tag = {account['player_tag']: account for account in result['rows'][0]['accounts']}
            overlap, within = by_tag['#P0'], by_tag['#Q0']
            for kind, count in [('regular_war', 2), ('cwl', 1)]:
                self.assertIsNone(overlap['observed_' + kind + '_attacks_since_start'])
                self.assertEqual(overlap['observed_' + kind + '_attacks_in_post_start_wars'], 1)
                self.assertEqual(len(overlap[kind + '_overlapping_war_participation']), count)
                self.assertTrue(all(war['attacks_since_start'] is None
                    for war in overlap[kind + '_overlapping_war_participation']))
                self.assertNotIn('latest_observed_' + kind + '_at', overlap)
                self.assertEqual(within['observed_' + kind + '_attacks_since_start'], 1)
            self.assertEqual(overlap['latest_observed_regular_war_end_at'],
                             datetime.fromtimestamp(1300, timezone.utc).isoformat())
            self.assertIn(OVERSEEING_TERRACE, context.state.source_channels)

    async def test_a_war_without_a_start_time_that_ended_months_after_the_start_counts_as_since_the_start(self):
        # War-log wars have only an end time. One that ended 90 days into the hibernation was fought in it;
        # one that ended an hour after its start may have had attacks before it.
        start, day = 1000, 86400
        regular = [regular_war('BEH', start + 90 * day, {'#P0': (2, [])}),
                   regular_war('BEH', start + 3600, {'#Q0': (1, [])})]
        context = _context()
        for role in context.member.roles:
            role.name = 'Access'
            role.is_default = lambda: False
        log = SimpleNamespace(**{**vars(context.source_message.channel), 'id': OVERSEEING_TERRACE})
        context.guild.get_channel_or_thread = lambda identifier: log if identifier == OVERSEEING_TERRACE else context.source_message.channel
        with TemporaryDirectory() as directory:
            record = await LeagueRecord(directory).initialize()
            context = with_record(replace(context, cwl_queries=record.queries(),
                hibernation_queries=HibernationQueries(lambda: {'41': {'hibernation_date': '<t:%d:F>' % start}}),
                account_links=SimpleNamespace(get_links_for_members=lambda ids: {
                    41: [{'player_tag': '#P0'}, {'player_tag': '#Q0'}]})))
            tool = next(tool for tool in TOOLS if tool.definition.name == 'read_hibernation')
            with regular_wars_from(*regular):
                result = await tool.handler(context, {'include_activity': True, 'activity_kinds': ['regular_war']})
        self.assertNotIn('error', result)
        by_tag = {account['player_tag']: account for account in result['rows'][0]['accounts']}
        self.assertEqual(by_tag['#P0']['observed_regular_war_attacks_since_start'], 2)
        self.assertEqual(by_tag['#P0']['regular_war_overlapping_war_participation'], [])
        self.assertIsNone(by_tag['#Q0']['observed_regular_war_attacks_since_start'])
        self.assertEqual(len(by_tag['#Q0']['regular_war_overlapping_war_participation']), 1)
