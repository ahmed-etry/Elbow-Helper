"""Any current guild role can be discovered and joined to its full holder cohort."""
from dataclasses import replace
from types import SimpleNamespace
import unittest

import discord

from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.reads.identity_reads import member_cohort
from elbow_helper.agent.research.members import find_discord_roles
from tests.agent.request_fixtures import flow_context as _context


class RoleDirectoryTests(unittest.IsolatedAsyncioTestCase):

    async def test_unconfigured_role_discovery_and_complete_holders(self):
        context = _context()
        role = SimpleNamespace(id=27, name='Raid Organizers', position=2, managed=False, mentionable=False,
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
        self.assertFalse(result['roles'][0]['asker_can_ping_here'])
        cohort = await member_cohort(context, {'basis': 'role_holders', 'role_ids': [27]})
        self.assertEqual({r['member_id'] for r in cohort['rows']}, {m.id for m in members if m.roles})
        self.assertEqual(result['roles'][0]['member_count'], len(cohort['rows']))
        # Role search identifies the role; its holders are read once, through the cohort.
        self.assertNotIn('members', result['roles'][0])
        absent = await find_discord_roles(context, {'query': 'Unknown role'})
        self.assertEqual(absent['matched_count'], 0)
        self.assertTrue(build_agent_tools()['find_discord_roles'].default_visible)
        self.assertTrue(all([r['role_id'] for r in member['roles']] == [27] for member in cohort['rows']))
