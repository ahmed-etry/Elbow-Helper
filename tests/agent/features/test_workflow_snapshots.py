"""One failure contract for the three private workflow snapshot adapters."""

import unittest

from tests.agent.features.examination import test_agent_examination as examination
from tests.agent.features.recruitment import test_agent_recruitment as recruitment
from tests.agent.features.support_tickets import test_agent_support as support


class WorkflowSnapshotFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_projection_retains_no_report_or_candidate_source(self):
        cases = (
            (examination.AgentExaminationToolTests, 'case_snapshot',
             examination.read_accessible_examination_cases),
            (recruitment.AgentRecruitmentToolTests, 'active_trial_snapshot',
             recruitment.read_active_recruitment_trials),
            (support.AgentSupportToolTests, 'metadata_snapshot',
             support.read_accessible_support_tickets),
        )
        for fixture_type, query_name, read in cases:
            with self.subTest(workflow=query_name):
                fixture = fixture_type()
                fixture.setUp()
                getattr(fixture.queries, query_name).side_effect = ValueError('synthetic malformed snapshot')
                result = await read(fixture.context, {})
                self.assertIn('error', result)
                self.assertEqual(fixture.context.state.reports, {})
                self.assertEqual(fixture.context.state.source_channels, set())
