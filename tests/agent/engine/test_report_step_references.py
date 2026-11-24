"""A bare step ID in a report field means that step's report; other unknown strings fail the plan check."""
import unittest

from tests.agent.request_fixtures import flow_context as _context

BARE = {'step': 'cohort', 'path': ['report_id']}


class ReportStepReferenceTests(unittest.IsolatedAsyncioTestCase):

    async def test_lost_report_still_says_read_it_again(self):
        from elbow_helper.agent.reads.projections import cohort_selection
        selection = await cohort_selection(_context(), 'was-held', 'member_ids')
        self.assertIn('Read it again', selection['error'])
