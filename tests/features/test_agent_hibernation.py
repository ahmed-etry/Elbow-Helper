from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.configuration.channels import HIBERNATION_LOG
from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.reports.hibernation import HibernationReport
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.reports.roles import RoleAccountReport
from elbow_helper.features.agent.tools.hibernation import (
    read_active_hibernation,
    read_active_hibernation_report,
)
from elbow_helper.features.hibernation.queries import (
    ActiveHibernationRecord,
    ActiveHibernationSnapshot,
)


def _snapshot(*, active=True):
    started = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    records = (
        ActiveHibernationRecord(
            42, started.isoformat(), int(started.timestamp()), "recorded",
        ),
        ActiveHibernationRecord(84, None, None, "unavailable"),
    ) if active else ()
    return ActiveHibernationSnapshot(
        "2026-09-17T12:00:00+00:00", len(records), 0, 2,
        sum(row.start_time_status == "unavailable" for row in records), records,
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


class AgentHibernationToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        member = SimpleNamespace(id=42, roles=[SimpleNamespace(id=next(iter(CORE)))])
        self.source = _Channel(100)
        self.log = _Channel(HIBERNATION_LOG)
        channels = {self.source.id: self.source, self.log.id: self.log}
        guild = SimpleNamespace(
            id=1, me=member, get_member=lambda _: member,
            get_channel_or_thread=lambda value: channels.get(value),
        )
        for channel in channels.values():
            channel.guild = guild
        self.queries = SimpleNamespace(active_snapshot=MagicMock(return_value=_snapshot()))
        self.context = AgentRequestContext(
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)),
            guild=guild, member=member,
            source_message=SimpleNamespace(
                channel=self.source, created_at=datetime.now(timezone.utc),
            ),
            account_links=None, clan_health=None, message_search=None,
            hibernation_queries=self.queries,
        )

    async def test_status_read_requires_log_access_and_excludes_private_fields(self):
        result = await read_active_hibernation(self.context, {})
        self.assertEqual(result["active_record_count"], 2)
        self.assertEqual(result["missing_start_time_count"], 1)
        self.assertEqual(result["records"][0]["member_id"], 42)
        self.assertEqual(self.context.state.source_channels, {HIBERNATION_LOG})
        self.assertNotIn("roles", str(result["records"]))
        self.assertNotIn("ticket", str(result["records"]))
        report = self.context.state.reports[result["report_id"]]
        self.assertIsInstance(report, HibernationReport)

        page = await read_active_hibernation_report(self.context, {
            "report_id": result["report_id"], "member_id": 84,
        })
        self.assertEqual(page["matched_count"], 1)
        self.queries.active_snapshot.assert_called_once_with()

    async def test_denied_log_access_prevents_private_state_read(self):
        self.log.allowed = False
        result = await read_active_hibernation(self.context, {})
        self.assertIn("error", result)
        self.queries.active_snapshot.assert_not_called()
        self.assertEqual(self.context.state.reports, {})

        self.log.allowed = True
        self.queries.active_snapshot.side_effect = ValueError("malformed")
        result = await read_active_hibernation(self.context, {})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.source_channels, set())

    async def test_empty_status_returns_coverage_without_artifact(self):
        self.queries.active_snapshot.return_value = _snapshot(active=False)
        result = await read_active_hibernation(self.context, {})
        self.assertEqual(result["active_record_count"], 0)
        self.assertIsNone(result["report_id"])
        self.assertEqual(self.context.state.reports, {})

    async def test_report_reuse_rechecks_log_access(self):
        result = await read_active_hibernation(self.context, {})
        self.log.allowed = False
        with self.assertRaises(AgentAccessLost):
            await read_active_hibernation_report(
                self.context, {"report_id": result["report_id"]},
            )

    async def test_budget_wrong_kind_and_foreign_guild_fail_closed(self):
        with patch("elbow_helper.features.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES", 1):
            result = await read_active_hibernation(self.context, {})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.reports, {})

        result = await read_active_hibernation(self.context, {})
        report = self.context.state.reports[result["report_id"]]
        self.context.state.reports["role"] = RoleAccountReport("role", "now", (), ())
        self.context.state.reports["foreign"] = HibernationReport(
            "foreign", 2, HIBERNATION_LOG, report.snapshot,
        )
        for report_id in ("missing", "role", "foreign"):
            value = await read_active_hibernation_report(
                self.context, {"report_id": report_id},
            )
            self.assertIn("error", value)


if __name__ == "__main__":
    unittest.main()
