"""Date windows select recorded events from real recruitment state queries."""
from dataclasses import replace
from datetime import datetime
import unittest

from elbow_helper.agent.reads.workflow_reads import TOOLS
from elbow_helper.features.recruitment.queries import RecruitmentQueries
from tests.agent.engine.test_agent_plan_flow import _context


class WorkflowEventDateTests(unittest.IsolatedAsyncioTestCase):
    async def test_creation_and_trial_end_on_opposite_sides_of_window_remain_separate(self):
        created = '2026-09-05T00:00:00+00:00'
        ended = '2026-10-05T00:00:00+00:00'
        payload = {'91': {'applicant_id': 41,
            'created_at': int(datetime.fromisoformat(created).timestamp()),
            'resolved_at': int(datetime.fromisoformat(ended).timestamp()), 'message_id': 101}}
        context = replace(_context(), recruitment_queries=RecruitmentQueries(
            dict, reminder_loader=lambda: payload))
        context.guild.threads = []
        tool = next(tool for tool in TOOLS if tool.definition.name == 'read_recruitment_pipeline')
        for stage in ('trial_followups', 'pipeline'):
            for after, before, kind, at in [
                ('2026-09-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00', 'record_created', created),
                (ended, '2026-11-01T00:00:00+00:00', 'trial_ended', ended),
            ]:
                with self.subTest(stage=stage, kind=kind):
                    result = await tool.handler(context, {'stage': stage, 'after': after, 'before': before})
                    self.assertNotIn('error', result)
                    self.assertEqual(len(result['rows']), 1)
                    row = result['rows'][0]
                    self.assertEqual([(event['event_kind'], event['event_at'])
                        for event in row['period_evidence']], [(kind, at)])
                    self.assertEqual(row['undated_evidence'], [])
                    self.assertTrue(result['period_selection_complete'])
                    if stage == 'pipeline':
                        self.assertEqual(row['timeline'], row['period_evidence'])
            excluded = await tool.handler(context, {'stage': stage,
                'after': '2026-09-01T00:00:00+00:00', 'before': created})
            self.assertEqual(excluded['rows'], [])
