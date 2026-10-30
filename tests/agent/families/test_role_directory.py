"""Any current guild role can be discovered and joined to its full holder cohort."""
from dataclasses import replace
from types import SimpleNamespace
import unittest

import discord

from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.families.identity_reads import member_cohort
from elbow_helper.agent.research.members import find_discord_roles
from elbow_helper.agent.plan.checker import check_plan
from elbow_helper.agent.families.model_view import family_model_view
from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.roles import AGENT_TESTER_ROLE_ID
from agent.engine.test_agent_plan_flow import _context


class RoleDirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_requested_roles_survive_with_only_configured_extras_on_opt_in(self):
        context = _context()
        home_id = CLANS['BES'].member_role_id
        roles = [SimpleNamespace(id=identifier, name=f'Role {identifier}')
                 for identifier in (27, 28, home_id, AGENT_TESTER_ROLE_ID)]
        holder = SimpleNamespace(id=101, display_name='Synthetic holder', roles=roles, joined_at=None)
        context.guild.roles = roles
        context.guild.members = [context.member, holder]
        context.guild.get_member = lambda identifier: holder if identifier == holder.id else context.member
        for selector in ({'role_ids': [27]}, {'role_name': 'Role 27'},
                         {'role_ids': [27, AGENT_TESTER_ROLE_ID], 'role_match': 'all'}):
            selected = {27, AGENT_TESTER_ROLE_ID} if selector.get('role_match') == 'all' else {27}
            for include_roles in (False, True):
                with self.subTest(selector=selector, include_roles=include_roles):
                    result = await member_cohort(context, {'basis': 'role_holders', **selector,
                                                          'include_roles': include_roles})
                    expected = selected | ({home_id} if include_roles else set())
                    visible = family_model_view({**result, 'family': 'read_member_cohort'})
                    for value in (result, visible):
                        self.assertEqual([row['member_id'] for row in value['rows']], [101])
                        self.assertEqual({role['role_id'] for role in value['rows'][0]['roles']}, expected)

    async def test_unconfigured_role_discovery_and_complete_holders(self):
        context = _context()
        role = SimpleNamespace(id=27, name='Raid Organizers', position=2, managed=False,
                               permissions=discord.Permissions.none(), is_default=lambda: False)
        members = [SimpleNamespace(id=i, display_name=f'Member {i}', joined_at=None,
                                  roles=[role] if i % 2 else []) for i in range(1, 102)]
        guild = context.guild
        guild.roles = [role]
        guild.members = [context.member, *members]
        guild.get_member = lambda identifier: next((m for m in guild.members if m.id == identifier), None)
        # Keep the requester's canonical identity distinct from synthetic holders.
        context = replace(context, member=guild.get_member(41))
        result = await find_discord_roles(context, {'query': 'Raid'})
        self.assertEqual(result['roles'][0]['role_id'], 27)
        cohort = await member_cohort(context, {'basis': 'role_holders', 'role_ids': [27]})
        self.assertEqual({r['member_id'] for r in cohort['rows']}, {m.id for m in members if m.roles})
        self.assertEqual(result['roles'][0]['member_count'], len(cohort['rows']))
        self.assertEqual(result['roles'][0]['members'], [
            {'member_id': m.id, 'display_name': m.display_name} for m in members if m.roles])
        absent = await find_discord_roles(context, {'query': 'Unknown role'})
        self.assertEqual(absent['matched_count'], 0)
        self.assertTrue(build_agent_tools()['find_discord_roles'].default_visible)
        visible = family_model_view({**cohort, 'family': 'read_member_cohort'})
        self.assertTrue(all([r['role_id'] for r in member['roles']] == [27] for member in visible['rows']))

    def test_any_role_can_be_resolved_and_joined_in_one_visible_plan(self):
        registry = {name: tool for name, tool in build_agent_tools().items() if tool.default_visible}
        plan = {'goal': 'Read current guild assignments', 'effort': 'low', 'output': 'text', 'steps': [
            {'id': 'roles', 'capability': 'find_discord_roles', 'arguments': {'query': 'Raid Organizers'}, 'depends_on': []},
            {'id': 'holders', 'capability': 'read_member_cohort', 'arguments': {
                'basis': 'role_holders', 'role_ids': {'step': 'roles', 'path': ['roles', '*', 'role_id']}},
             'depends_on': ['roles']}]}
        checked = check_plan(plan, registry)
        self.assertTrue(checked.ok, checked.error)
