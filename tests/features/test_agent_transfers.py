from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.reports.roles import RoleAccountReport
from elbow_helper.features.agent.tools.transfers import (
    read_pending_transfer_report,
    read_pending_transfer_requests,
)
from elbow_helper.features.agent.reports.transfer import TransferQueueReport
from elbow_helper.features.clan_transfers.config import CLAN_TRANSFER_QUEUES
from elbow_helper.features.clan_transfers.queries import (
    PendingTransferRequest,
    PendingTransferSnapshot,
    TransferQueueRegistration,
    TransferQueueSnapshot,
)


def _request(member_id):
    created = datetime(2026, 9, 17, 5, tzinfo=timezone.utc)
    expires = datetime(2026, 9, 17, 17, tzinfo=timezone.utc)
    return PendingTransferRequest(
        member_id, created.isoformat(), int(created.timestamp()),
        expires.isoformat(), int(expires.timestamp()),
    )


def _snapshot(*, active=True):
    queues = []
    for code, config in CLAN_TRANSFER_QUEUES.items():
        if code == "BEH":
            pending = (_request(42),) if active else ()
            queues.append(TransferQueueSnapshot(
                code, config["thread_id"], len(pending) + 1, 1, pending,
            ))
        elif code == "BEC":
            queues.append(TransferQueueSnapshot(
                code, config["thread_id"], 1, 0, (_request(84),),
            ))
        else:
            queues.append(TransferQueueSnapshot(code, config["thread_id"], 0, 0, ()))
    return PendingTransferSnapshot(
        "2026-09-17T12:00:00+00:00", 12, tuple(queues),
    )


class _Channel:
    def __init__(self, channel_id, *, allowed=True):
        self.id = channel_id
        self.allowed = allowed
        self.guild = None

    def permissions_for(self, actor):
        del actor
        return SimpleNamespace(
            view_channel=self.allowed, read_message_history=self.allowed,
        )


class AgentTransferToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        member = SimpleNamespace(id=42, roles=[SimpleNamespace(id=next(iter(CORE)))])
        self.source = _Channel(100)
        self.beh = _Channel(CLAN_TRANSFER_QUEUES["BEH"]["thread_id"])
        self.be4 = _Channel(CLAN_TRANSFER_QUEUES["BE4"]["thread_id"])
        self.bec = _Channel(CLAN_TRANSFER_QUEUES["BEC"]["thread_id"], allowed=False)
        channels = {row.id: row for row in (self.source, self.beh, self.be4, self.bec)}
        guild = SimpleNamespace(
            id=1, me=member, get_member=lambda _: member,
            get_channel_or_thread=lambda value: channels.get(value),
        )
        for channel in channels.values():
            channel.guild = guild
        self.snapshot = _snapshot()
        registrations = tuple(TransferQueueRegistration(
            code, config["thread_id"],
        ) for code, config in CLAN_TRANSFER_QUEUES.items())
        self.queries = SimpleNamespace(
            queue_registrations=MagicMock(return_value=registrations),
            pending_snapshot=MagicMock(side_effect=self._selected_snapshot),
        )
        self.context = AgentRequestContext(
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)),
            guild=guild, member=member,
            source_message=SimpleNamespace(
                channel=self.source, created_at=datetime.now(timezone.utc),
            ),
            account_links=None, clan_health=None, message_search=None,
            transfer_queries=self.queries,
        )

    def _selected_snapshot(self, *, clan_codes):
        return PendingTransferSnapshot(
            self.snapshot.observed_at, self.snapshot.request_ttl_hours,
            tuple(queue for queue in self.snapshot.queues
                  if queue.clan_code in clan_codes),
        )

    async def test_read_filters_queues_by_current_source_access_and_retains_all_rows(self):
        result = await read_pending_transfer_requests(self.context, {})

        self.assertEqual(result["accessible_queue_count"], 2)
        self.assertEqual(result["omitted_inaccessible_count"], 6)
        self.assertEqual(result["pending_request_count"], 1)
        self.assertEqual(result["expired_stored_count"], 1)
        self.assertEqual(result["requests"][0]["member_id"], 42)
        self.assertEqual(
            self.context.state.source_channels,
            {self.beh.id, self.be4.id},
        )
        report = self.context.state.reports[result["report_id"]]
        self.assertIsInstance(report, TransferQueueReport)
        self.assertNotIn(84, [row["member_id"] for row in result["requests"]])

        page = await read_pending_transfer_report(self.context, {
            "report_id": result["report_id"], "clan_code": "beh",
            "member_id": 42,
        })
        self.assertEqual(page["matched_count"], 1)
        self.queries.pending_snapshot.assert_called_once_with(
            clan_codes=("BEH", "BE4"),
        )

    async def test_empty_accessible_queues_return_coverage_without_artifact(self):
        self.snapshot = _snapshot(active=False)
        result = await read_pending_transfer_requests(self.context, {})
        self.assertEqual(result["pending_request_count"], 0)
        self.assertIsNone(result["report_id"])
        self.assertEqual(result["expired_stored_count"], 1)
        self.assertEqual(self.context.state.reports, {})

    async def test_malformed_snapshot_does_not_retain_candidate_sources(self):
        self.queries.pending_snapshot.side_effect = ValueError("malformed")
        result = await read_pending_transfer_requests(self.context, {})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.source_channels, set())

    async def test_report_reuse_rechecks_queue_source_access(self):
        result = await read_pending_transfer_requests(self.context, {})
        self.beh.allowed = False
        with self.assertRaises(AgentAccessLost):
            await read_pending_transfer_report(
                self.context, {"report_id": result["report_id"]},
            )

    async def test_budget_wrong_kind_and_foreign_guild_fail_closed(self):
        with patch("elbow_helper.features.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES", 1):
            result = await read_pending_transfer_requests(self.context, {})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.reports, {})

        result = await read_pending_transfer_requests(self.context, {})
        report = self.context.state.reports[result["report_id"]]
        self.context.state.reports["role"] = RoleAccountReport("role", "now", (), ())
        self.context.state.reports["foreign"] = TransferQueueReport(
            "foreign", 2, report.observed_at, report.request_ttl_hours,
            report.registered_queue_count, report.omitted_inaccessible_count,
            report.queues,
        )
        for report_id in ("missing", "role", "foreign"):
            value = await read_pending_transfer_report(
                self.context, {"report_id": report_id},
            )
            self.assertIn("error", value)


if __name__ == "__main__":
    unittest.main()
