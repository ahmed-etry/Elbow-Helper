"""Planner-selected inputs survive source joins, export and the model boundary."""

from dataclasses import dataclass, replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.reads import identity_reads
from elbow_helper.agent.reads import war_tools as war_reads
from elbow_helper.agent.reads.access import TOOLS as ACCESS_TOOLS
from elbow_helper.agent.tool_support.model_view import family_model_view
from agent.cwl_helpers import ResultQueries
from agent.engine.test_agent_plan_flow import _context


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
                raise ValueError('Synthetic missing history')
            return Snapshot((Score('#P0', 1), Score('#Q0', 2)))

        context = replace(_context(), cwl_queries=ResultQueries(ass_wars=score))
        with patch('elbow_helper.features.cwl.results.CwlResults.selection', return_value={'war_ids': ['war']}):
            result = await war_reads.cwl_results(context, {'metric': 'ass', 'clan_codes': ['BEH', 'BEM'],
                'season': '2026-09', 'account_tags': ['#Q0']})
        self.assertEqual(result['ranked_population'], 2)
        self.assertEqual(result['rows'], [{'player_tag': '#Q0', 'rank': 2, 'clan_code': 'BEH'}])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['missing_sources'][0]['clan_code'], 'BEM')

    async def test_account_role_projection_is_explicit_and_preserves_unknown_owners(self):
        context = _context()
        role = SimpleNamespace(id=CLANS['BES'].member_role_id, name='Home role', is_default=lambda: False)
        context.member.roles.append(role)
        for item in context.member.roles:
            item.name = 'Role'
            item.is_default = lambda: False
        original = context.guild.get_member
        context.guild.get_member = lambda key: original(key) if key in (41, 999) else None
        context = replace(context, account_links=SimpleNamespace(
            get_links_for_members=lambda ids: {identifier: [{'player_tag': tag}] for identifier, tag in (
                (41, '#P0'), (42, '#Q0')) if identifier in ids},
            get_links_by_tags=lambda tags: {
            '#P0': {'discord_user_id': 41, 'player_tag': '#P0'},
            '#Q0': {'discord_user_id': 42, 'player_tag': '#Q0'}, '#P2': None}))
        tool = next(t for t in identity_reads.TOOLS if t.definition.name == 'read_member_accounts')
        data = {tag: {'status': 'ok', 'data': {'name': tag}} for tag in ('#P0', '#Q0', '#P2')}
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value=data)):
            hidden = await tool.handler(context, {'account_tags': list(data)})
            shown = await tool.handler(context, {'account_tags': list(data), 'include_roles': True})
        self.assertTrue(all('roles' not in row for row in family_model_view(hidden)['rows']))
        rows = {row['player_tag']: row for row in family_model_view(shown)['rows']}
        members = {row['member_id']: row for row in family_model_view(shown)['members']}
        self.assertIn(role.id, {r['role_id'] for r in members[41]['roles']})
        self.assertEqual(members[42]['membership_status'], 'not_in_guild')
        self.assertEqual(rows['#Q0']['member_id'], 42)
        self.assertIsNone(rows['#Q0']['member_name'])
        self.assertIsNone(rows['#P2']['member_id'])


    async def test_role_matrix_uses_live_channel_permissions_and_keeps_role_only_caveat(self):
        context = _context()
        role = SimpleNamespace(id=12, name='Role A', permissions=discord.Permissions(ban_members=True))
        context.guild.roles = [role]
        channel = context.source_message.channel
        channel.name = 'Synthetic room'
        channel.permissions_for = lambda actor: discord.Permissions(
            view_channel=actor.id != role.id, read_message_history=True)
        result = await ACCESS_TOOLS[0].handler(context, {'role_names': ['Role A']})
        row = result['rows'][0]
        self.assertFalse(row['view_channel'])
        self.assertIn('ban_members', row['guild_permissions'])
        self.assertTrue(row['role_only'])
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
