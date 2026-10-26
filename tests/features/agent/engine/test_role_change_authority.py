"""GAPS-FIX-2 regression: RoleChangeAuthorityTests."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import discord
from elbow_helper.features.agent.discord_actions.roles import read_role_change_preview
from elbow_helper.features.agent.engine.service import AgentService
from features.agent.engine.test_agent_plan_flow import _context, _Model, _Session


class RoleChangeAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_denied_requester_sees_operator_names_without_mutation_capabilities(self):
        context = _context()
        context.member.guild_permissions = discord.Permissions.none()
        operator = SimpleNamespace(id=101, name='Role staff', permissions=discord.Permissions(manage_roles=True))
        admin = SimpleNamespace(id=102, name='Server admins', permissions=discord.Permissions(administrator=True))
        context.guild.roles.extend([operator, admin])
        context = replace(context, bot=SimpleNamespace(tree=object()))
        model = _Model(_Session([], []))
        with patch('elbow_helper.features.agent.engine.service.build_command_tools', return_value=({}, {})):
            flow, _ = AgentService(model)._start_flow(question='Remove my obsolete clan roles',
                local_context='', context=context, conversation_history='')
        self.assertNotIn('remove_discord_roles', flow.registry)
        self.assertNotIn('add_discord_roles', flow.registry)
        self.assertIn('read_discord_role_change_preview', flow.registry)
        self.assertFalse(json.loads(flow.answer_context)['role_change_access']['requester_allowed'])
        self.assertIn('Role staff', model.request['prompt'])
        self.assertIn('Server admins', model.request['prompt'])
        self.assertFalse(context.state.proposed_changes)

        role = SimpleNamespace(id=123, name='Old clan', permissions=discord.Permissions.none(),
            managed=False, position=1, is_default=lambda: False)
        context.guild.get_role = lambda _: role
        context.member.remove_roles = AsyncMock()
        with patch('elbow_helper.features.agent.discord_actions.roles.managed_role_commands', AsyncMock(return_value={})):
            result = await read_role_change_preview(context, {'role_id': 123,
                'member_ids': [context.member.id], 'operation': 'remove'})
        self.assertFalse(result['members'][0]['allowed'])
        self.assertEqual(result['members'][0]['refusal'], 'Changing roles requires Manage Roles permission.')
        self.assertFalse(result['change_attempted'])
        self.assertFalse(result['confirmation_prepared'])
        context.member.remove_roles.assert_not_awaited()
        self.assertFalse(context.state.proposed_changes)
