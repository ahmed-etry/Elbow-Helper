"""Large participation cohorts remain readable through the real family boundary."""
import unittest

from elbow_helper.agent.reads.snapshots import family_tool
from tests.agent.request_fixtures import flow_context as _context


class ParticipationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_live_reads_have_time_to_return_and_retain_partial_observations(self):
        import time
        from dataclasses import replace
        from elbow_helper.agent.engine.budgets import TOOL_TIMEOUT_SECONDS
        context = replace(_context(), deadline_monotonic=time.monotonic() + 200)
        original_deadline = context.deadline_monotonic
        async def source(read_context, arguments):
            self.assertLessEqual(read_context.deadline_monotonic - time.monotonic(),
                                 TOOL_TIMEOUT_SECONDS - 5)
            return {'rows': [{'player_tag': '#P0', 'attacks_used': 4,
                              'live_status': 'timeout'}]}
        tool = family_tool('read_war_participation', 'Read participation.', {}, source)
        result = await tool.handler(context, {})
        self.assertEqual(result['rows'][0]['attacks_used'], 4)
        self.assertIn(result['report_id'], context.state.reports)
        self.assertEqual(context.deadline_monotonic, original_deadline)
