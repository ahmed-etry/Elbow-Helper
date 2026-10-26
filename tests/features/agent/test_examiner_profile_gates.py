"""Examiner readers retain panel provenance and respect destination scope."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from elbow_helper.configuration import roles
from elbow_helper.configuration.channels import EXAMINATION_PANEL_THREAD
from elbow_helper.features.agent.access import AgentAccessLost, require_evidence_access
from elbow_helper.features.agent.capabilities.examination.examiner_profile import (
    prepare_examiner_profile,
)
from elbow_helper.features.agent.actions.contracts import ActionRefused
from elbow_helper.features.agent.engine.capability_contract import compile_capability_call
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.engine.tool_call import execute_tool
from elbow_helper.features.agent.models import AgentTurnState
from features.agent import test_evidence_scope as fixtures


class ExaminerProfileGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = fixtures.ScopeTests()
        fixture.setUp()
        self.context = replace(fixture.context, private_pass=True)
        self.panel = fixtures.Channel(EXAMINATION_PANEL_THREAD, self.context.guild,
                                      {10, 99}, level=roles.EXAMINERS | roles.LEAD_PLUS)
        self.context.guild.channels.append(self.panel)
        self.workflow = SimpleNamespace(
            can_edit_examiner_profile=lambda _: False,
            examiner_profile_snapshot=Mock(return_value={
                "th_levels": [18], "status": "Active", "timezone": "UTC",
                "availability": "Evenings", "profile_complete": True,
            }),
            has_examiner_profile=lambda _: True,
            examiner_roster_snapshot=Mock(return_value=[{"member_id": 10}]),
        )
        self.context = replace(self.context, bot=SimpleNamespace(get_cog=lambda _: self.workflow))
        self.tools = build_agent_tools()

    async def read(self, name):
        tool = self.tools[name]
        return json.loads(await execute_tool(
            name=name, handler=tool.handler, arguments={}, context=self.context,
            capability_scope=compile_capability_call(tool, {}),
        ))

    async def test_all_approved_roles_read_both_tools_through_engine(self):
        for role_id in roles.EXAMINERS | roles.LEAD_PLUS:
            for name in ("read_my_examiner_profile", "read_examiner_roster"):
                with self.subTest(role_id=role_id, name=name):
                    self.context = replace(self.context, state=AgentTurnState())
                    self.context.member.roles = [fixtures.Role(roles.AGENT_TESTER_ROLE_ID),
                                                 fixtures.Role(role_id)]
                    result = await self.read(name)
                    self.assertNotIn("error", result)
                    self.assertEqual(result["panel_channel_id"], self.panel.id)
                    self.assertIn(self.panel.id, self.context.state.source_channels)
                    self.assertIn("examiner", self.context.state.required_access)

    async def test_scope_and_unauthorized_roles_block_before_snapshot(self):
        for private, role_id in ((True, next(iter(roles.RECRUITERS))),
                                 (False, next(iter(roles.EXAMINERS)))):
            self.context = replace(self.context, private_pass=private, state=AgentTurnState())
            self.context.source_message.channel = self.context.guild.channels[1]
            self.context.member.roles = [fixtures.Role(roles.AGENT_TESTER_ROLE_ID),
                                         fixtures.Role(role_id)]
            self.assertIn("error", await self.read("read_examiner_roster"))
        self.workflow.examiner_roster_snapshot.assert_not_called()

    async def test_panel_permission_loss_and_role_loss_invalidate_retained_data(self):
        self.context.member.roles.append(fixtures.Role(next(iter(roles.EXAMINERS))))
        self.assertNotIn("error", await self.read("read_examiner_roster"))
        self.panel.viewers.remove(99)
        with self.assertRaises(AgentAccessLost):
            await require_evidence_access(self.context)
        self.panel.viewers.add(99)
        self.context.member.roles = [fixtures.Role(roles.AGENT_TESTER_ROLE_ID)]
        with self.assertRaises(AgentAccessLost):
            await require_evidence_access(self.context)

    async def test_read_authorization_preserves_profile_edit_permission(self):
        self.context.member.roles.append(fixtures.Role(roles.CO_APPLICANT_ROLE_ID))
        with self.assertRaises(ActionRefused):
            await prepare_examiner_profile(self.context, {"status": "Away"})
