"""Hibernation joins real completed-war evidence without inventing attack dates."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.channels import HIBERNATION_LOG
from elbow_helper.features.agent.families.workflow_reads import TOOLS
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.cwl.queries import CwlQueries
from elbow_helper.features.hibernation.queries import HibernationQueries
from features.agent.engine.test_agent_plan_flow import _context


class HibernationWarTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_overlapping_wars_keep_unknown_attack_counts_and_explicit_war_end_dates(self):
        with TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / 'wars.db')
            repository.initialize()
            with closing(sqlite3.connect(repository.path)) as connection, connection:
                for kind, identity, start, end in [
                    ('REG', '#C0', 900, 1100), ('REG', '#C2', 1000, 1200),
                    ('REG', '#C8', None, 1300), ('REG', '#C9', 800, 999),
                    ('CWL', '#R0', 900, 1100), ('CWL', '#R2', 1000, 1200)]:
                    connection.execute('''INSERT INTO wars(war_id,war_type,clan_code,clan_tag,
                        opponent_tag,state,start_ts,end_ts,last_seen_ts)
                        VALUES(?,?,?,?,?,?,?,?,?)''',
                        (identity, kind, 'BEH', CLANS['BEH'].tag, '#Q2', 'warEnded', start, end, 1))
                    for tag in ('#P0', '#Q0') if start == 1000 else ('#P0',):
                        connection.execute('''INSERT INTO war_roster_members(war_id,clan_code,
                            player_tag,player_name,attacks_expected,attacks_used,roster_state,captured_ts)
                            VALUES(?,?,?,?,?,?,?,?)''',
                            (identity, 'BEH', tag, tag, 2, 1, 'warEnded', 1))
            context = _context()
            for role in context.member.roles:
                role.name = 'Access'
                role.is_default = lambda: False
            log = SimpleNamespace(**{**vars(context.source_message.channel), 'id': HIBERNATION_LOG})
            context.guild.get_channel_or_thread = lambda identifier: log if identifier == HIBERNATION_LOG else context.source_message.channel
            context = replace(context, cwl_queries=CwlQueries(repository, lambda: {}),
                hibernation_queries=HibernationQueries(lambda: {'41': {'hibernation_date': '<t:1000:F>'}}),
                account_links=SimpleNamespace(get_links_for_members=lambda ids: {
                    41: [{'player_tag': '#P0'}, {'player_tag': '#Q0'}]}))
            tool = next(tool for tool in TOOLS if tool.definition.name == 'read_hibernation')
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
