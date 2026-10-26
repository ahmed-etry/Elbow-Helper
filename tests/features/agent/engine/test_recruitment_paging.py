"""Recruitment history continuation keeps its typed stage and complete date window."""
import unittest

from elbow_helper.features.agent.engine.answer_inputs import read_handles
from elbow_helper.features.agent.families.workflow_reads import TOOLS
from elbow_helper.features.agent.plan.checker import valid_arguments


class RecruitmentPagingTests(unittest.TestCase):
    def test_each_history_stage_retains_a_valid_scoped_continuation(self):
        tool = next(t for t in TOOLS if t.definition.name == 'read_recruitment_pipeline')
        selection = {'after': '2026-09-01T00:00:00Z', 'before': '2026-10-01T00:00:00Z', 'member_ids': [55]}
        for stage in ('ticket_log', 'acceptance_history'):
            result = {'family': 'read_recruitment_pipeline', 'channel_id': 123, 'stage': stage,
                      'selection': {**selection, 'stage': stage}, 'source_next_cursor': 'opaque',
                      'coverage': {'requested_after': selection['after'], 'requested_before': selection['before'],
                                   'next_cursor': 'opaque', 'reached_requested_start': False}}
            args = read_handles({'source': result})['source']['source_next_arguments']
            self.assertEqual(args, {**selection, 'stage': stage, 'source_cursor': 'opaque'})
            self.assertTrue(valid_arguments(args, tool.definition.parameters))
            last = {**result, 'source_next_cursor': None,
                    'coverage': {**result['coverage'], 'next_cursor': None, 'reached_requested_start': True}}
            handles = read_handles({'first': result, 'last': last})
            self.assertNotIn('source_next_arguments', handles.get('first', {}))
