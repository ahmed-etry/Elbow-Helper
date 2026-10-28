"""The real roster removal gate and menu remain discoverable through writer evidence."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.configuration.roles import LEAD_PLUS
from elbow_helper.infrastructure.discord.command_registry import setup as setup_command_registry
from elbow_helper.features.agent.commands.help_tool import read_bot_command_help
from elbow_helper.features.agent.engine.answer_inputs import answer_evidence
from elbow_helper.features.agent.families.reference_reads import TOOLS
from elbow_helper.features.rosters.cog import Rosters
from elbow_helper.features.rosters.ui.views import RosterSettingsView
from contracts.test_architecture_boundaries import _RegistryBot
from features.agent.engine.test_agent_plan_flow import _context


class SignupRemovalReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_real_operator_gets_registered_help_and_named_writer_controls(self):
        bot = _RegistryBot()
        await setup_command_registry(bot)
        for operator in sorted(LEAD_PLUS):
            with self.subTest(operator=operator):
                context = replace(_context(), bot=bot, private_pass=True)
                roles = [SimpleNamespace(id=identifier, name='Operator ' + str(index))
                         for index, identifier in enumerate(sorted(LEAD_PLUS))]
                context.guild.roles.extend(roles)
                context.member.roles.append(next(role for role in roles if role.id == operator))
                self.assertTrue(Rosters.is_lead(context.member))
                search = await read_bot_command_help(context, {'query': 'Remove accounts'})
                self.assertIn('/roster post', [row['path'] for row in search['commands']])
                help_result = await read_bot_command_help(context, {'command_path': '/roster post'})
                self.assertIn('Remove accounts', help_result['details'])
                report = await TOOLS[0].handler(context, {'reference_kind': 'bot_workflows'})
                workflow = next(row for row in report['rows'] if row['workflow'] == 'roster_signup_management')
                permission = workflow['removal']['permission']
                self.assertEqual(set(permission['any_role_ids']), LEAD_PLUS)
                self.assertTrue(permission['requester_allowed'])
                projected = answer_evidence({'workflow': report}, guild=context.guild)['workflow']
                facts = next(row['roster_signup_management'] for row in projected['rows']
                             if 'roster_signup_management' in row)
                removal = facts['removal']
                self.assertEqual(removal['control'], 'Remove accounts')
                self.assertEqual(removal['confirm_control'], 'Remove')
                self.assertTrue(removal['requester_allowed'])
                self.assertEqual(removal['operator_roles'], [sorted(role.name for role in roles)])
                cog = SimpleNamespace(is_lead=Rosters.is_lead, show_roster_removal_picker=AsyncMock())
                view = RosterSettingsView(cog, 7, is_open=True, buttons_hidden=False)
                select = view.children[0]
                self.assertEqual(next(option.label for option in select.options if option.value == 'remove'),
                                 removal['control'])
                interaction = SimpleNamespace(user=context.member)
                await view.run_action(interaction, 'remove')
                cog.show_roster_removal_picker.assert_awaited_once_with(interaction, 7)

    async def test_core_alone_has_no_management_permission_or_staff_help(self):
        bot = _RegistryBot()
        await setup_command_registry(bot)
        context = replace(_context(), bot=bot, private_pass=True)
        self.assertFalse(Rosters.is_lead(context.member))
        help_result = await read_bot_command_help(context, {'command_path': '/roster post'})
        self.assertEqual(help_result['commands'], [])
        report = await TOOLS[0].handler(context, {'reference_kind': 'bot_workflows'})
        workflow = next(row for row in report['rows'] if row['workflow'] == 'roster_signup_management')
        self.assertFalse(workflow['removal']['permission']['requester_allowed'])
        cog = SimpleNamespace(is_lead=Rosters.is_lead, show_roster_removal_picker=AsyncMock())
        view = RosterSettingsView(cog, 7, is_open=True, buttons_hidden=False)
        response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock())
        await view.run_action(SimpleNamespace(user=context.member, response=response), 'remove')
        cog.show_roster_removal_picker.assert_not_awaited()
        response.send_message.assert_awaited_once()
