"""Reports that unfinished plan steps reference stay retained, or fail with a clear reason."""
import unittest

from elbow_helper.agent.reads.projections import cohort_selection
from tests.agent.request_fixtures import flow_context as _context

ROWS = [{'member_id': index, 'pad': 'x' * 120} for index in range(1, 4)]


class PendingReportRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_evicted_report_without_a_pending_reference_is_still_unavailable(self):
        context = _context()
        selection = await cohort_selection(context, 'missing', 'member_ids')
        self.assertEqual(selection['cause'], 'report_unavailable')
