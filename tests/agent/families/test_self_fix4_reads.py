"""Home roles, selected-cohort activity and scoped references reach the writer."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.families import identity_reads as reads
from elbow_helper.agent.families import reference_reads
from elbow_helper.agent.families.model_view import family_model_view
from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.configuration.roles import LEAD
from agent.engine.test_agent_plan_flow import _context
from agent.families import test_named_role_cohorts, test_reference


class HomeClanTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_home_survives_other_in_game_clan_and_hidden_cosmetic_roles(self):
        context = _context()
        home = SimpleNamespace(id=CLANS['BES'].member_role_id, name='Home BES', is_default=lambda: False)
        owner = SimpleNamespace(id=42, display_name='Member', roles=[home], joined_at=None)
        context.guild.get_member = lambda identifier: context.member if identifier == 41 else owner if identifier == 42 else None
        context = replace(context, account_links=SimpleNamespace(
            get_links_for_members=lambda ids: {42: [{'player_tag': '#P0'}]}))
        profile = {'#P0': {'status': 'ok', 'data': {'clan': {'tag': CLANS['BEH'].tag, 'name': CLANS['BEH'].name}}}}
        tool = next(t for t in reads.TOOLS if t.definition.name == 'read_member_accounts')
        with patch.object(reads, 'profiles', AsyncMock(return_value=profile)):
            result = await tool.handler(context, {'member_ids': [42]})
        visible = family_model_view(result)
        self.assertNotIn('roles', visible['rows'][0])
        row = answer_evidence({'member': visible})['member']['rows'][0]
        self.assertEqual(row['clan']['tag'], CLANS['BEH'].tag)
        row = visible['members'][0]
        self.assertEqual([c['clan_code'] for c in row['home_clans']], ['BES'])
        self.assertEqual(row['home_clan_status'], 'resolved')

    async def test_unlinked_members_still_have_roles_but_missing_owners_remain_unknown(self):
        context = _context()
        role = SimpleNamespace(id=CLANS['BES'].member_role_id, name='BES', is_default=lambda: False)
        context.member.roles.append(role)
        for existing in context.member.roles:
            existing.name = 'Role'
            existing.is_default = lambda: False
        context = replace(context, account_links=SimpleNamespace(get_links_for_members=lambda ids: {}))
        result = await reads.member_accounts(context, {'member_ids': [41]})
        self.assertEqual(result['members'][0]['home_clan_status'], 'resolved')
        self.assertIsNone(result['rows'][0]['player_tag'])
        self.assertEqual(reads.member_fields(None, 42)['home_clan_status'], 'not_in_guild')
        self.assertEqual(reads.member_fields(None, 42)['home_clans'], [])

    def test_multiple_roles_are_not_resolved_using_roster_or_role_order(self):
        owner = SimpleNamespace(id=42, display_name='Member', roles=[
            SimpleNamespace(id=CLANS[code].member_role_id, name=code, is_default=lambda: False)
            for code in ('BES', 'BEH')])
        row = reads.member_fields(owner, 42)
        self.assertEqual({c['clan_code'] for c in row['home_clans']}, {'BEH', 'BES'})
        self.assertEqual(row['home_clan_status'], 'multiple')
        owner.roles = []
        self.assertEqual(reads.member_fields(owner, 42)['home_clan_status'], 'no_home_role')


class RoleActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_role_ids_and_home_scope_select_accounts_before_metadata_activity_join(self):
        context = test_named_role_cohorts.NamedRoleTests().context()
        links = Mock(return_value={101: [{'player_tag': '#P0'}]})
        context = replace(context, account_links=SimpleNamespace(get_links_for_members=links),
            achievement_queries=SimpleNamespace(member_activity=lambda ids: {}),
            message_search=SimpleNamespace(history_page=AsyncMock(side_effect=AssertionError('No channel reads'))))
        args = {'basis': 'role_holders', 'role_ids': [27], 'clan_codes': ['BEH'],
                'include_activity': True, 'after': '2025-12-01T00:00:00+00:00'}
        with patch.object(reads, 'profiles', AsyncMock(return_value={
                '#P0': {'status': 'ok', 'data': {'name': 'Account', 'donations': 12}}})), \
             patch('elbow_helper.agent.families.cohort_activity.selected_wars',
                   AsyncMock(return_value={'rows': [], 'coverage': {}})):
            result = await reads.member_cohort(context, args)
        self.assertEqual(links.call_args.args[0], [101])
        self.assertEqual([r['member_id'] for r in result['rows']], [101])
        self.assertEqual(result['selected_role_ids'], [27])
        self.assertEqual(result['rows'][0]['account_tags'], ['#P0'])
        visible = family_model_view({**result, 'family': 'read_member_cohort'})
        self.assertEqual(visible['rows'][0]['donations'], 12)
        self.assertEqual(visible['rows'][0]['discord_activity_status'], 'no_record')
        self.assertIsNone(result['rows'][0]['war_activity']['attacks_used'])
        self.assertFalse(result['activity_coverage']['historical_collection_complete'])

class ScopedReferenceTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        context = test_reference.ReferenceFamilyTests().reference_context()
        role = SimpleNamespace(id=next(iter(LEAD)), name='Co-Leader')
        context.member.roles.append(role)
        context.guild.roles.append(role)
        return context

    async def test_scoped_requester_gets_real_reader_roles_without_excluded_title_or_body(self):
        context = self.context()
        blocked = await reference_reads.community_reference(context, {'titles': ['Restricted']})
        self.assertEqual(blocked['rows'], [])
        self.assertTrue(blocked['available_privately'])
        self.assertEqual(blocked['access_limits'][0]['reader_roles'][0]['name'], 'Co-Leader')
        self.assertNotIn('Restricted', str(blocked))
        self.assertNotIn('private body', str(blocked))
        projected = answer_evidence({'guide': blocked})
        self.assertEqual(projected['guide']['access_limits'], blocked['access_limits'])

    async def test_unauthorized_title_matches_unknown_and_unrelated_exclusion_does_not_taint_read(self):
        context = test_reference.ReferenceFamilyTests().reference_context()
        hidden = await reference_reads.community_reference(context, {'titles': ['Restricted']})
        unknown = await reference_reads.community_reference(context, {'titles': ['Unknown']})
        self.assertEqual(hidden, unknown)
        context = self.context()
        await reference_reads.community_reference(context, {'titles': ['Restricted']})
        result = await reference_reads.community_reference(context, {'titles': ['Public']})
        self.assertFalse(result['available_privately'])
        self.assertEqual(result['access_limits'], [])
        self.assertTrue(result['rows'][0]['body'])
