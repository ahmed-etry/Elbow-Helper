from datetime import datetime, timezone
import unittest

from elbow_helper.features.role_connections.queries import (
    RoleConnectionQueries, connection_matches,
)


class RoleConnectionQueriesTests(unittest.TestCase):
    def setUp(self):
        self.observed = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)

    def test_snapshot_validates_rules_and_preserves_cycles(self):
        state = [
            {
                "id": "alpha", "target_role_id": 10,
                "all": [{"has": 20}], "any": [{"not": 30}],
            },
            {
                "id": "cycle-a", "target_role_id": 20,
                "all": [{"has": 40}], "any": [],
            },
            {
                "id": "cycle-b", "target_role_id": 40,
                "all": [{"has": 20}], "any": [],
            },
            {"id": "broken", "target_role_id": True, "all": [], "any": []},
        ]
        snapshot = RoleConnectionQueries(
            lambda: state, clock=lambda: self.observed,
        ).snapshot()

        self.assertEqual(snapshot.observed_at, self.observed.isoformat())
        self.assertEqual(snapshot.total_entries, 4)
        self.assertEqual(snapshot.malformed_indexes, (3,))
        self.assertEqual(
            [rule.connection_id for rule in snapshot.rules if rule.cyclic],
            ["cycle-a", "cycle-b"],
        )
        self.assertEqual(len(snapshot.state_fingerprint), 64)

    def test_matching_uses_existing_all_and_any_semantics(self):
        rules = RoleConnectionQueries(lambda: [{
            "id": "alpha", "target_role_id": 10,
            "all": [{"has": 20}, {"not": 30}],
            "any": [{"has": 40}, {"not": 50}],
        }]).snapshot().rules

        self.assertTrue(connection_matches({20, 40}, rules[0]))
        self.assertTrue(connection_matches({20}, rules[0]))
        self.assertFalse(connection_matches({20, 30, 40}, rules[0]))
        self.assertFalse(connection_matches({20, 50}, rules[0]))

    def test_state_changes_change_fingerprint_without_mutating_source(self):
        state = [{
            "id": "alpha", "target_role_id": 10,
            "all": [{"has": 20}], "any": [],
        }]
        queries = RoleConnectionQueries(lambda: state)
        before = queries.snapshot()
        state[0]["all"].append({"not": 30})
        after = queries.snapshot()

        self.assertNotEqual(before.state_fingerprint, after.state_fingerprint)
        self.assertEqual(len(before.rules[0].all_conditions), 1)
        self.assertEqual(len(after.rules[0].all_conditions), 2)

    def test_unavailable_or_oversized_state_fails_closed(self):
        with self.assertRaises(RuntimeError):
            RoleConnectionQueries(lambda: None).snapshot()
        oversized = [
            {"id": str(index), "target_role_id": index + 1}
            for index in range(1_001)
        ]
        with self.assertRaises(RuntimeError):
            RoleConnectionQueries(lambda: oversized).snapshot()


if __name__ == "__main__":
    unittest.main()
