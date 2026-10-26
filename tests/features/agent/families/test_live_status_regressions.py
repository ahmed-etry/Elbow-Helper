"""Stored workflow status survives missing channels and departed members."""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.families import identity_reads, workflow_reads
from elbow_helper.features.recruitment.queries import RecruitmentQueries
from features.agent.engine.test_agent_plan_flow import _context


class WorkflowStatusRegressionTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        context = replace(_context(), private_pass=True)
        present = SimpleNamespace(id=100, display_name='Present', roles=[], joined_at=None)
        context.guild.members = [context.member, present]
        context.guild.get_member = lambda identifier: next((m for m in context.guild.members if m.id == identifier), None)
        for member in context.guild.members:
            for role in member.roles:
                role.name = 'Core'
                role.is_default = lambda: False
        state = {str(200 + index): {'start': '2026-10-01T00:00:00+00:00', 'days': 7,
                                   'applicant_id': 100 + index} for index in range(14)}
        return replace(context, recruitment_queries=RecruitmentQueries(lambda: state,
            clock=lambda: datetime(2026, 10, 3, tzinfo=timezone.utc)))

    async def test_status_reads_all_fourteen_without_resolving_ticket_channels(self):
        context = self.context()
        tool = next(t for t in workflow_reads.TOOLS if t.definition.name == 'read_recruitment_pipeline')
        with patch.object(workflow_reads, 'accessible_message_channel', AsyncMock(
                side_effect=AssertionError('Status must not depend on ticket survival'))):
            result = await tool.handler(context, {'stage': 'active_trials'})
        self.assertEqual(len(result['rows']), 14)
        self.assertEqual({r['applicant_member_id'] for r in result['rows']}, set(range(100, 114)))

    async def test_departed_trial_members_keep_acceptance_dates(self):
        context = self.context()
        tool = next(t for t in identity_reads.TOOLS if t.definition.name == 'read_member_cohort')
        result = await tool.handler(context, {'basis': 'accepted_recruitment'})
        self.assertEqual(len(result['rows']), 14)
        self.assertEqual(sum(r['membership_status'] == 'not_in_guild' for r in result['rows']), 13)
        self.assertTrue(all(r['accepted_at'] == '2026-10-01T00:00:00+00:00' for r in result['rows']))

    async def test_denied_recruitment_is_an_error_without_an_empty_snapshot(self):
        from elbow_helper.configuration.roles import AGENT_TESTER_ROLE_ID
        context = self.context()
        context.member.roles = [SimpleNamespace(id=AGENT_TESTER_ROLE_ID)]
        tool = next(t for t in workflow_reads.TOOLS if t.definition.name == 'read_recruitment_pipeline')
        result = await tool.handler(context, {'stage': 'active_trials'})
        self.assertEqual(result['cause'], 'scope_refused')
        self.assertNotIn('rows', result)
        self.assertFalse(context.state.reports)
