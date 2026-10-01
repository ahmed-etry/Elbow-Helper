"""Standing rules are durable, member-owned, and claimed once."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from elbow_helper.features.agent.actions.repository import AgentActionRepository


class StandingRepositoryTests(unittest.TestCase):
    def test_finishing_a_run_preserves_pause_and_cancel(self):
        for kind in ("request", "watcher"):
            for status in ("paused", "cancelled"):
                identifier = self.repository.create_standing(kind=kind, guild_id=1,
                    requester_id=2, destination_channel_id=3, rule={"request": "Synthetic"}, next_at=1)
                self.assertTrue(self.repository.claim_standing(kind=kind, identifier=identifier,
                    version=0, owner="worker", now=2))
                self.assertTrue(self.repository.set_standing_status(kind=kind, identifier=identifier,
                    requester_id=2, status=status))
                self.assertTrue(self.repository.finish_standing(kind=kind, identifier=identifier,
                    owner="worker", next_at=3))
                row = self.repository.standing(kind=kind, identifier=identifier)
                self.assertEqual(row["status"], status)
                self.assertIsNone(row["lease_owner"])

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repository = AgentActionRepository(Path(directory.name) / "actions.sqlite3")

    def test_saved_request_claim_and_management(self):
        identifier = self.repository.create_standing(
            kind="request", guild_id=1, requester_id=2,
            destination_channel_id=3, rule={"request": "Check things"},
            next_at=1000, now=900,
        )
        self.assertEqual(self.repository.list_standing(requester_id=2)[0]["rule"],
                         {"request": "Check things"})
        self.assertIsNone(self.repository.standing(kind="request", identifier=identifier,
                                                  requester_id=4))
        due = self.repository.due_standing(kind="request", guild_id=1, now=1000)
        self.assertEqual(len(due), 1)
        self.assertTrue(self.repository.claim_standing(
            kind="request", identifier=identifier, version=due[0]["version"],
            owner="worker", now=1000,
        ))
        self.assertFalse(self.repository.claim_standing(
            kind="request", identifier=identifier, version=due[0]["version"],
            owner="other", now=1000,
        ))
        self.assertTrue(self.repository.set_standing_status(
            kind="request", identifier=identifier, requester_id=2, status="paused",
        ))
        interrupted = self.repository.recover_standing_leases(guild_id=1, now=1001)
        self.assertEqual([item["request_id"] for item in interrupted], [identifier])
        self.assertEqual(self.repository.standing(kind="request", identifier=identifier)["status"],
                         "paused")
        self.assertTrue(self.repository.set_standing_status(
            kind="request", identifier=identifier, requester_id=2, status="active",
        ))
        self.assertTrue(self.repository.claim_standing(
            kind="request", identifier=identifier,
            version=self.repository.standing(kind="request", identifier=identifier)["version"],
            owner="worker", now=1001,
        ))
        self.assertTrue(self.repository.finish_standing(
            kind="request", identifier=identifier, owner="worker", next_at=2000,
        ))
        self.assertEqual(self.repository.due_standing(kind="request", guild_id=1,
                                                       now=1001), [])
        self.assertTrue(self.repository.set_standing_status(
            kind="request", identifier=identifier, requester_id=2, status="paused",
        ))
        self.assertTrue(self.repository.set_standing_status(
            kind="request", identifier=identifier, requester_id=2, status="cancelled",
        ))
        self.assertEqual(self.repository.list_standing(requester_id=2), [])

    def test_watcher_result_and_member_timezone(self):
        identifier = self.repository.create_standing(
            kind="watcher", guild_id=1, requester_id=2,
            destination_channel_id=3, rule={"condition": "Changed"},
            next_at=1000, now=900,
        )
        self.assertTrue(self.repository.claim_standing(
            kind="watcher", identifier=identifier, version=0, owner="worker", now=1000,
        ))
        self.assertTrue(self.repository.finish_standing(
            kind="watcher", identifier=identifier, owner="worker", next_at=2000,
            last_result={"value": 1}, holding=True,
        ))
        row = self.repository.standing(kind="watcher", identifier=identifier)
        self.assertEqual(row["last_result"], {"value": 1})
        self.assertEqual(row["holding"], 1)
        self.repository.set_member_timezone(2, "Europe/Paris")
        self.assertEqual(self.repository.member_timezone(2), "Europe/Paris")

    def test_replacement_rejects_a_stale_preview(self):
        identifier = self.repository.create_standing(
            kind="request", guild_id=1, requester_id=2,
            destination_channel_id=3, rule={"request": "First"},
            next_at=1000, now=900,
        )
        self.assertTrue(self.repository.replace_standing(
            kind="request", identifier=identifier, requester_id=2,
            rule={"request": "Second"}, destination_channel_id=3,
            next_at=2000, expected_version=0,
        ))
        self.assertFalse(self.repository.replace_standing(
            kind="request", identifier=identifier, requester_id=2,
            rule={"request": "Old preview"}, destination_channel_id=3,
            next_at=2000, expected_version=0,
        ))
        self.assertEqual(self.repository.standing(kind="request", identifier=identifier)["rule"],
                         {"request": "Second"})
