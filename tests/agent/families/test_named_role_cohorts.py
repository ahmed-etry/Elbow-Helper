"""Named responsibilities never fall back to configured clan leadership."""
from types import SimpleNamespace
import unittest

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.reads.identity_reads import member_cohort
from agent.engine.test_agent_plan_flow import _context


class NamedRoleTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        context = _context()
        def role(identifier, name):
            return SimpleNamespace(id=identifier, name=name, is_default=lambda: False)
        war = role(27, 'War Co')
        home = role(CLANS['BEH'].member_role_id, 'BEH')
        lead = role(CLANS['BEH'].leadership_role_id, 'Leadership BEH')
        participant = role(CLANS['BEH'].war_role_id, 'War BEH')
        members = [SimpleNamespace(id=i, display_name=f'Member {i}', joined_at=None, roles=held)
                   for i, held in ((101, [war, home]), (102, [war]),
                                   (103, [lead, home]), (104, [participant, home]))]
        context.guild.roles = [war, home, lead, participant]
        context.guild.members = [context.member, *members]
        context.guild.get_member = lambda identifier: next(
            (m for m in context.guild.members if m.id == identifier), None)
        return context

    async def test_named_role_returns_all_holders_without_leadership_or_war_participants(self):
        result = await member_cohort(self.context(), {'basis': 'role_holders', 'role_name': 'War Co'})
        self.assertEqual({r['member_id'] for r in result['rows']}, {101, 102})
        self.assertEqual(result['selected_role_ids'], [27])
        self.assertTrue(all([role['role_id'] for role in row['roles']] == [27]
                            for row in result['rows']))

    async def test_clan_scope_intersects_named_holders_with_home_role(self):
        result = await member_cohort(self.context(), {'basis': 'role_holders',
            'role_name': 'war co', 'clan_codes': ['BEH']})
        self.assertEqual([r['member_id'] for r in result['rows']], [101])
        self.assertEqual(result['selected_role_ids'], [27])
        self.assertEqual(result['clan_member_role_ids'], [CLANS['BEH'].member_role_id])

    async def test_duplicate_and_absent_names_require_resolution_without_fallback(self):
        for duplicate in (False, True):
            context = self.context()
            if duplicate:
                context.guild.roles.append(SimpleNamespace(id=28, name='War Co'))
            result = await member_cohort(context, {'basis': 'role_holders',
                'role_name': 'War Co' if duplicate else 'Absent role', 'clan_codes': ['BEH']})
            self.assertEqual(result['status'], 'needs_input')
            self.assertEqual(result['rows'], [])
            self.assertEqual(len(result['roles']), 2 if duplicate else 0)
