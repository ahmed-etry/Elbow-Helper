"""A retained result is not complete when its requested selection is unresolved."""
import unittest

from elbow_helper.agent.plan.results import model_result

class SelectionCoverageTests(unittest.TestCase):
    def test_completed_or_unspecified_selection_retains_ordinary_completion(self):
        for metadata in ({'selection_complete': True}, {}):
            with self.subTest(metadata=metadata):
                result = model_result({'rows': [{'value': 7}], **metadata})
                self.assertEqual(result['flags']['status'], 'complete')
