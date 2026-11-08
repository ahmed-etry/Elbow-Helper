from datetime import datetime, timezone
import unittest

from elbow_helper.features.clan_transfers.config import CLAN_TRANSFER_QUEUES
from elbow_helper.features.clan_transfers.queries import ClanTransferQueries


class ClanTransferQueryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
        self.state = {"clans": {
            code: {"pending": []} for code in CLAN_TRANSFER_QUEUES
        }}
        self.queries = ClanTransferQueries(lambda: self.state, clock=lambda: self.now)

    def test_snapshot_separates_active_and_expired_without_mutating_state(self):
        self.state["clans"]["BEH"]["pending"] = [
            {"user_id": 2, "created_at": "2026-09-17T06:00:00+00:00"},
            {"user_id": 1, "created_at": "2026-09-17T05:00:00+00:00"},
            {"user_id": 3, "created_at": "2026-09-16T12:00:00+00:00"},
        ]

        snapshot = self.queries.pending_snapshot(
            clan_codes=tuple(CLAN_TRANSFER_QUEUES),
        )
        queue = next(row for row in snapshot.queues if row.clan_code == "BEH")

        self.assertEqual(snapshot.observed_at, "2026-09-17T12:00:00+00:00")
        self.assertEqual(snapshot.request_ttl_hours, 12)
        self.assertEqual(queue.stored_request_count, 3)
        self.assertEqual(queue.expired_stored_count, 1)
        self.assertEqual([row.member_id for row in queue.pending], [1, 2])
        self.assertEqual(queue.pending[0].expires_at, "2026-09-17T17:00:00+00:00")
        self.assertEqual(len(self.state["clans"]["BEH"]["pending"]), 3)


    def test_snapshot_reads_only_the_authorized_queue_selection(self):
        self.state["clans"]["BEH"]["pending"] = [
            {"user_id": 1, "created_at": "2026-09-17T06:00:00+00:00"},
        ]
        self.state["clans"]["BEC"]["pending"] = [
            {"user_id": 2, "created_at": "private-malformed-row"},
        ]

        registrations = self.queries.queue_registrations()
        snapshot = self.queries.pending_snapshot(clan_codes=("BEH",))

        self.assertEqual(len(registrations), len(CLAN_TRANSFER_QUEUES))
        self.assertEqual([queue.clan_code for queue in snapshot.queues], ["BEH"])
        self.assertEqual(snapshot.queues[0].pending[0].member_id, 1)


if __name__ == "__main__":
    unittest.main()
