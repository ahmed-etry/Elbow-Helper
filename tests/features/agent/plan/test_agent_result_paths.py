"""Unresolved reference diagnostics contain no source values."""
import unittest
from elbow_helper.features.agent.plan.executor import execute_plan


class UnresolvedReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_warning_names_step_and_path_without_result_values(self):
        for source in ({"roles": []}, {"roles": [{}]}, {"roles": None}, {}):
            calls = []
            async def run(step, arguments, earlier):
                calls.append(step["id"])
                return {**source, "secret": "synthetic-private-value"}
            plan = {"steps": [
                {"id": "source", "arguments": {}, "depends_on": []},
                {"id": "consumer", "arguments": {"nested": [{"step": "source", "path": ["roles", 0, "role_id"]}]},
                 "depends_on": ["source"]},
            ]}
            with self.subTest(source=source), self.assertLogs(
                "elbow_helper.features.agent.plan.executor", level="WARNING",
            ) as logs:
                results = await execute_plan(plan, run)
            self.assertEqual(results["consumer"], {"error": "A required earlier result is unavailable."})
            self.assertEqual(calls, ["source"])
            self.assertEqual(len(logs.records), 1)
            self.assertIn("consumer", logs.output[0])
            self.assertIn("source", logs.output[0])
            self.assertIn("['roles', 0, 'role_id']", logs.output[0])
            self.assertNotIn("synthetic-private-value", logs.output[0])
