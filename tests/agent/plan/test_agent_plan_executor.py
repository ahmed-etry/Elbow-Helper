"""Dependency execution and cancellation use synthetic steps."""

import unittest

from elbow_helper.agent.plan.executor import UnresolvedReferenceError, resolve_arguments
class PlanExecutorTests(unittest.IsolatedAsyncioTestCase):
    def test_rows_aliases_preserve_nested_wildcard_expansion(self):
        groups = [{"items": [{"name": f"Synthetic {group}:{index}"} for index in range(10)]}
                  for group in range(2)]
        result = resolve_arguments({"names": {"step": "source", "path": [
            "groups", "*", "items", "rows", "*", "name",
        ]}}, {"source": {"groups": groups}})
        expected = [row["name"] for group in groups for row in group["items"]]
        self.assertEqual(result["names"], expected)


    def test_nested_wildcards_flatten_only_expanded_indexes(self):
        source = {"groups": [{"members": [{"accounts": [{"tag": "#P0"}, {"tag": "#P2"}]}]},
                             {"members": []},
                             {"members": [{"accounts": []}, {"accounts": [{"tag": "#P8"}]}]}]}
        reference = {"step": "source", "path": ["groups", "*", "members", "*", "accounts", "*", "tag"]}
        self.assertEqual(resolve_arguments({"tags": reference}, {"source": source}),
                         {"tags": ["#P0", "#P2", "#P8"]})
        self.assertEqual(resolve_arguments({"tags": ["#P9", reference]}, {"source": source}),
                         {"tags": ["#P9", "#P0", "#P2", "#P8"]})
        self.assertEqual(resolve_arguments({"rows": {"step": "source", "path": ["groups", "*", "members"]}},
                                           {"source": source}),
                         {"rows": [group["members"] for group in source["groups"]]})
        source["groups"][0]["members"] = None
        with self.assertRaises(UnresolvedReferenceError):
            resolve_arguments({"tags": reference}, {"source": source})

    def test_action_result_reference_waits_for_confirmed_run(self):
        reference = {"step": "created", "path": ["target_id"]}
        arguments = {"target_id": reference}
        self.assertEqual(resolve_arguments(arguments, {
            "created": {"status": "confirmation_required"},
        }), arguments)
        self.assertEqual(resolve_arguments(arguments, {
            "created": {"target_id": 7},
        }), {"target_id": 7})
