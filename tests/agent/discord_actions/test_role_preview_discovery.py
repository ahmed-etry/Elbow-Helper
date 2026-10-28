"""Role discovery exposes a read-only delta without preparing actions."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
import discord

from elbow_helper.agent.discord_actions.roles import discord_role_tools
from elbow_helper.agent.families.catalogue import discovery_tool
from elbow_helper.agent.models import AgentCapabilityEffect
from agent.engine.test_agent_plan_flow import _context


class RolePreviewDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_elder_name_resolves_and_preview_does_not_prepare_change(self):
        context = _context()
        elder = SimpleNamespace(id=123, name='Elder', managed=False, position=1,
                                permissions=discord.Permissions.none(), is_default=lambda: False)
        target = SimpleNamespace(id=42, roles=[], display_name='Target')
        context.guild.roles = [elder]
        context.guild.get_role = lambda identifier: elder if identifier == elder.id else None
        context.guild.get_member = lambda identifier: target if identifier == target.id else context.member
        context.guild.owner_id = context.member.id
        context.member.guild_permissions = SimpleNamespace(manage_roles=True)
        context = replace(context, hibernation_queries=SimpleNamespace(is_hibernating=lambda _: False))
        tools = {t.definition.name: replace(t, specialist_group='changes') for t in discord_role_tools()}
        active = {}
        opened = await discovery_tool(tools, active, lambda: None).handler(context,
            {'group': 'changes', 'intent': 'inspect'})
        preview = active['read_discord_role_change_preview']
        self.assertIs(preview.effect, AgentCapabilityEffect.READ)
        self.assertNotIn('add_discord_roles', active)
        self.assertEqual(opened['roles'], [{'role_id': elder.id, 'name': 'Elder'}])
        result = await preview.handler(context, {'role_name': 'Elder', 'member_ids': [42], 'operation': 'add'})
        self.assertEqual(result['members'][0]['add_role_ids'], [elder.id])
        self.assertEqual(result['members'][0]['current_role_ids'], [])
        self.assertFalse(result['change_attempted'])
        self.assertFalse(result['confirmation_prepared'])
        self.assertFalse(context.state.proposed_changes)
