"""A retained result is not complete when its requested selection is unresolved."""
from copy import deepcopy
import unittest

from elbow_helper.agent.plan.results import model_result
from elbow_helper.agent.engine.answer_inputs import answer_evidence


class SelectionCoverageTests(unittest.TestCase):
    def test_partial_selection_keeps_readable_facts_and_marks_the_writer_gap(self):
        raw = {'rows': [{'value': 7}], 'complete_snapshot': True,
            'selection_complete': False, 'coverage': {'query_complete': True}}
        original = deepcopy(raw)
        result = model_result(raw)
        self.assertEqual(result['flags']['status'], 'partial')
        self.assertEqual(result['rows'], original['rows'])
        writer = answer_evidence({'facts': result})['facts']
        self.assertFalse(writer['evidence_complete'])
        self.assertFalse(writer['selection_complete'])
        self.assertEqual(raw, original)

    def test_completed_or_unspecified_selection_retains_ordinary_completion(self):
        for metadata in ({'selection_complete': True}, {}):
            with self.subTest(metadata=metadata):
                result = model_result({'rows': [{'value': 7}], **metadata})
                self.assertEqual(result['flags']['status'], 'complete')
