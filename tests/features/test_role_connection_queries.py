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


if __name__ == "__main__":
    unittest.main()
