"""Home roles, selected-cohort activity and scoped references reach the writer."""
from dataclasses import replace
from types import SimpleNamespace
import unittest

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.reads import identity_reads as reads
from elbow_helper.agent.reads import reference as reference_reads
from elbow_helper.configuration.roles import LEAD
from tests.agent.request_fixtures import flow_context as _context
from tests.agent.reads import test_reference


class HomeClanTests(unittest.IsolatedAsyncioTestCase):
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


class ScopedReferenceTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        context = test_reference.ReferenceFamilyTests().reference_context()
        role = SimpleNamespace(id=next(iter(LEAD)), name='Co-Leader')
        context.member.roles.append(role)
        context.guild.roles.append(role)
        return context


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
