"""Routing leaves room to resolve a subject before reading its measurements."""
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.engine.budgets import FAMILY_LIMITS
from elbow_helper.agent.engine.routing import RouteDecision
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.families.core import family_tool
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan


class DependentFamilyReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_plan_subject_read_measurement_and_answer_share_the_family_budget(self):
        accounts = AsyncMock(return_value={'rows': [{'member_id': 41, 'home_clans': [{'clan_code': 'BEM'}]}]})
        attendance = AsyncMock(return_value={'rows': [{'player_tag': '#P0', 'missed': 1}]})
        registry = {tool.definition.name: tool for tool in (
            family_tool('read_member_accounts', 'Read identities.', {}, accounts),
            family_tool('read_war_attendance', 'Read measurements.', {
                'clan_codes': {'type': 'array', 'items': {'type': 'string'}},
                'war_type': {'type': 'string'}, 'latest_completed': {'type': 'integer'}}, attendance))}
        registry['hidden'] = replace(registry['read_member_accounts'], default_visible=False)
        first = {'id': 'subject', 'capability': 'read_member_accounts', 'arguments': {}, 'depends_on': []}
        second = {'id': 'measurement', 'capability': 'read_war_attendance', 'arguments': {
            'clan_codes': ['BEM'], 'war_type': 'REG', 'latest_completed': 1}, 'depends_on': []}
        session = _Session([
            replace(_model_step(_plan([first])), usage=AgentUsage(25_000, 1000)),
            replace(_model_step(_plan([second])), usage=AgentUsage(13_000, 400)),
            AgentStep('One attack was missed.', (), AgentUsage(14_000, 100))], [])
        with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
            flow, _ = AgentService(_Model(session))._start_flow(question='Review the selected clan.',
                local_context='', context=_context(), conversation_history='',
                route=RouteDecision(social=False, usage=AgentUsage(1446, 73)))
        self.assertEqual(await flow.run(), 'One attack was missed.')
        self.assertEqual(flow.budget.request_limits, FAMILY_LIMITS)
        self.assertEqual(flow.budget.spent_rounds, 4)
        self.assertEqual([call[1] for call in session.calls], [True, True, False])
        self.assertEqual(flow.runner.tool_calls, 2)
        attendance.assert_awaited_once()
        self.assertFalse(flow.budget.request_can_research(1000))
        self.assertGreater(flow.budget.request_output_limit(1000, allow_tools=False), 0)
