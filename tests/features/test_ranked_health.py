"""Synthetic incident thresholds, recovery, and independent polling deadlines."""

import logging
import unittest

from elbow_helper.features.ranked_stats.health import CollectionHealth


LOGGER = logging.getLogger(__name__)
KIND = "battle log"


class CollectionHealthTests(unittest.TestCase):
    def setUp(self):
        self.health = CollectionHealth(LOGGER)

    def poll(self, statuses, kind=KIND):
        for tag, status in statuses.items():
            self.health.record(tag, kind, status, ok=status == 200)
        self.health.report()

    def test_transient_failures_warn_at_third_poll_once_then_rearm_after_recovery(self):
        for status, detail in ((None, "no response"), (500, "status 500"),
                               (503, "status 503"), (429, "status 429")):
            with self.subTest(status=status):
                self.setUp()
                with self.assertLogs(LOGGER, level="DEBUG") as logs:
                    self.poll({"#P0": status})
                    self.health.report()  # No due requests must not advance the streak.
                    self.poll({"#P0": status})
                self.assertTrue(all(record.levelname == "DEBUG" for record in logs.records))
                self.assertIn(detail, logs.output[0])
                with self.assertLogs(LOGGER, level="WARNING") as logs:
                    for _ in range(5):
                        self.poll({"#P0": status})
                self.assertEqual(len(logs.output), 1)
                self.assertIn("3 consecutive polls", logs.output[0])
                with self.assertLogs(LOGGER, level="INFO") as logs:
                    self.poll({"#P0": 200})
                    self.poll({"#P0": 200})
                self.assertEqual(len(logs.output), 1)
                self.assertIn("recovered", logs.output[0])
                with self.assertLogs(LOGGER, level="WARNING") as logs:
                    for _ in range(3):
                        self.poll({"#P0": status})
                self.assertEqual(len(logs.output), 1)

    def test_success_resets_short_streak_without_recovery_note(self):
        with self.assertNoLogs(LOGGER, level="INFO"):
            for _ in range(5):
                self.poll({"#P0": None})
                self.poll({"#P0": 503})
                self.poll({"#P0": 200})

    def test_outage_warns_once_without_per_account_spam_and_recovers_once(self):
        failed = {"#P0": None, "#P2": 503, "#P8": 500}
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            for _ in range(6):
                self.poll(failed)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("outage: 3 of 3", logs.output[0])
        with self.assertNoLogs(LOGGER, level="INFO"):
            self.poll({"#P0": 200})  # Independent deadlines: two accounts still failing.
            self.health.report()
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.poll({"#P2": 200, "#P8": 200})
            self.poll({tag: 200 for tag in failed})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("outage recovered", logs.output[0])
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            self.poll(failed)
        self.assertEqual(len(logs.output), 1)

    def test_outage_requires_majority_and_three_observed_accounts(self):
        with self.assertNoLogs(LOGGER, level="WARNING"):
            self.poll({"#P0": 503, "#P2": 503})
            self.poll({"#P8": 200, "#P9": 200})  # Exactly half is not a majority.
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            self.poll({"#P8": None})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("outage: 3 of 4", logs.output[0])

    def test_remaining_persistent_account_warns_after_outage_recovers(self):
        with self.assertLogs(LOGGER):
            for _ in range(3):
                self.poll({"#P0": 503, "#P2": 503, "#P8": 503})
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.poll({"#P2": 200, "#P8": 200})
        self.assertEqual([record.levelname for record in logs.records], ["INFO", "WARNING"])
        self.assertIn("#P0", logs.output[1])
        with self.assertNoLogs(LOGGER, level="INFO"):
            self.poll({"#P0": 503})

    def test_missing_player_warns_once_across_endpoints_until_validated_recovery(self):
        with self.assertLogs(LOGGER) as logs:
            for _ in range(4):
                self.health.record("#P0", "player snapshot", 404)
                self.poll({"#P0": 404})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("does not exist (status 404)", logs.output[0])
        with self.assertNoLogs(LOGGER, level="INFO"):
            self.poll({"#P0": 200}, "player snapshot")
            self.poll({"#P0": None})  # A different failure is not recovery.
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.poll({"#P0": 200})
            self.poll({"#P0": 200})
        self.assertEqual(len(logs.output), 1)
        with self.assertLogs(LOGGER) as logs:
            self.poll({"#P0": 404})
        self.assertEqual(len(logs.output), 1)

    def test_missing_players_do_not_trigger_outage(self):
        with self.assertLogs(LOGGER) as logs:
            self.poll({"#P0": 404, "#P2": 404, "#P8": 404})
        self.assertEqual(len(logs.output), 3)
        self.assertFalse(any("outage" in line for line in logs.output))

    def test_permission_failure_warns_immediately_and_once(self):
        with self.assertLogs(LOGGER) as logs:
            for _ in range(5):
                self.poll({"#P0": 403})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("status 403", logs.output[0])

    def test_malformed_success_response_is_not_recovery(self):
        with self.assertLogs(LOGGER) as logs:
            self.health.record("#P0", KIND, 200)  # HTTP success, invalid body.
            self.health.report()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("unavailable", logs.output[0])
        with self.assertLogs(LOGGER, level="INFO"):
            self.poll({"#P0": 200})

    def test_profile_success_does_not_reset_battle_failure_streak(self):
        with self.assertLogs(LOGGER) as logs:
            for _ in range(3):
                self.health.record("#P0", "player snapshot", 200, ok=True)
                self.poll({"#P0": 503})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("battle log", logs.output[0])

    def test_unlink_discards_failures_without_claiming_recovery(self):
        with self.assertLogs(LOGGER):
            for _ in range(3):
                self.poll({"#P0": 503})
        with self.assertNoLogs(LOGGER, level="INFO"):
            self.health.retain(set())
            self.health.report()
            self.poll({"#P0": 503})  # Relink starts a fresh streak.
        self.assertEqual(self.health.failures["#P0", KIND].consecutive, 1)
        self.assertFalse(self.health.failures["#P0", KIND].warned)


if __name__ == "__main__":
    unittest.main()
