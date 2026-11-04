"""Hibernation joins real completed-war evidence without inventing attack dates."""
from dataclasses import replace
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from elbow_helper.configuration.channels import HIBERNATION_LOG
from elbow_helper.agent.families.workflow_reads import TOOLS
from elbow_helper.features.hibernation.queries import HibernationQueries
from agent.engine.test_agent_plan_flow import _context
from agent.families.record_stub import regular_war, regular_wars_from, with_record
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
            log = SimpleNamespace(**{**vars(context.source_message.channel), 'id': HIBERNATION_LOG})
            context.guild.get_channel_or_thread = lambda identifier: log if identifier == HIBERNATION_LOG else context.source_message.channel
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
            self.assertIn(HIBERNATION_LOG, context.state.source_channels)
