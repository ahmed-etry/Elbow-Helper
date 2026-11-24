"""Planner-selected inputs survive source joins, export and the model boundary."""

from dataclasses import dataclass, replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.domain.cwl import CwlScoringIncomplete
from elbow_helper.agent.reads import war_tools as war_reads
from elbow_helper.agent.reads.access import TOOLS as ACCESS_TOOLS
from tests.agent.features.cwl.cwl_helpers import ResultQueries
from tests.agent.request_fixtures import flow_context as _context


class PlannerEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_clan_scoring_retains_success_when_another_clan_fails(self):
        @dataclass
        class Score:
            player_tag: str
            rank: int

        @dataclass
        class Snapshot:
            rows: tuple

        def score(clan_code, war_ids):
            if clan_code == 'BEM':
                raise CwlScoringIncomplete('Synthetic missing history')
            return Snapshot((Score('#P0', 1), Score('#Q0', 2)))

        context = replace(_context(), cwl_queries=ResultQueries(ass_wars=score))
        with patch('elbow_helper.features.cwl.results.CwlResults.selection', return_value={'war_ids': ['war']}):
            result = await war_reads.cwl_results(context, {'metric': 'ass', 'clan_codes': ['BEH', 'BEM'],
                'season': '2026-09', 'account_tags': ['#Q0']})
        self.assertEqual(result['ranked_population'], 2)
        self.assertEqual(result['rows'], [{'player_tag': '#Q0', 'rank': 2, 'clan_code': 'BEH'}])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['missing_sources'][0]['clan_code'], 'BEM')


    async def test_role_matrix_uses_live_channel_permissions_and_keeps_role_only_caveat(self):
        context = _context()
        role = SimpleNamespace(id=12, name='Role A', permissions=discord.Permissions(ban_members=True))
        context.guild.roles = [role]
        channel = context.source_message.channel
        channel.name = 'Synthetic room'
        channel.permissions_for = lambda actor: discord.Permissions(
            view_channel=actor.id != role.id, read_message_history=True)
        result = await ACCESS_TOOLS[0].handler(context, {'role_names': ['Role A']})
        actor = result['actors'][0]
        self.assertEqual(actor['actor_id'], role.id)
        self.assertIn('ban_members', actor['guild_permissions'])
        self.assertTrue(actor['role_only'])
        row = result['rows'][0]
        self.assertEqual(row['channel_id'], channel.id)
        self.assertEqual(row['view_channel'], [False])
        self.assertEqual(row['read_message_history'], [True])
        self.assertEqual(result['permission_sets'][row['channel_permissions'][0]], ['read_message_history'])
        self.assertIn('Role-only access does not establish', result['coverage'])
        self.assertIn(channel.id, context.state.source_channels)
        self.assertFalse(context.state.proposed_changes)

    async def test_role_matrix_refuses_an_unreadable_channel_without_retaining_a_report(self):
        context = _context()
        context.guild.roles = [SimpleNamespace(id=12, name='Role A')]
        with patch('elbow_helper.agent.reads.access.accessible_message_channel',
                   AsyncMock(return_value=None)):
            result = await ACCESS_TOOLS[0].handler(context, {'role_names': ['Role A'], 'channel_ids': [102]})
        self.assertEqual(result['cause'], 'scope_refused')
        self.assertFalse(context.state.reports)
