"""The real roster removal gate and menu remain discoverable through writer evidence."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.infrastructure.discord.command_registry import setup as setup_command_registry
from elbow_helper.agent.commands.help_tool import read_bot_command_help
from elbow_helper.agent.reads.reference import TOOLS
from elbow_helper.features.rosters.cog import Rosters
from elbow_helper.features.rosters.ui.views import RosterSettingsView
from tests.agent.commands.command_registry_fixture import CommandRegistryBot
from tests.agent.engine.test_agent_plan_flow import _context


class SignupRemovalReferenceTests(unittest.IsolatedAsyncioTestCase):

    async def test_core_alone_has_no_management_permission_or_staff_help(self):
        bot = CommandRegistryBot()
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
