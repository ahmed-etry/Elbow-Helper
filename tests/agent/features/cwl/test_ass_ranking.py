"""A bounded result cannot keep its earlier completeness flags."""

import json
import unittest

from elbow_helper.agent.engine.tool_call import bound_tool_result
from elbow_helper.agent.plan.results import model_result, model_view


class ResultFlagsTests(unittest.TestCase):
    def test_a_cut_result_does_not_keep_the_flags_it_had_before_the_cut(self):
        payload = {"records": [{"id": index, "text": "x" * 100} for index in range(400)]}
        content = json.dumps(model_view(model_result(payload)), ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(json.loads(content)["flags"], {"status": "complete", "truncated": False})
        result = json.loads(bound_tool_result(content, 4000))
        self.assertTrue(result["truncated"])
        self.assertEqual(result["flags"], {"status": "partial", "truncated": True})
