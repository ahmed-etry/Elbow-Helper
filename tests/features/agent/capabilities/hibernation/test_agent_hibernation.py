from datetime import datetime, timezone
from dataclasses import asdict
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.configuration.channels import HIBERNATION_LOG
from elbow_helper.configuration.roles import CORE, LEAD
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.capabilities.hibernation.report import HibernationReport
from elbow_helper.features.agent.models import AgentRequestContext
from features.agent.report_helpers import make_event_report
from elbow_helper.features.agent.capabilities.hibernation.reads import (
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
        member = SimpleNamespace(id=42, roles=[SimpleNamespace(id=next(iter(CORE))), SimpleNamespace(id=next(iter(LEAD)))])
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
        self.context = AgentRequestContext(private_pass=True,
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)),
            guild=guild, member=member,
            source_message=SimpleNamespace(
                channel=self.source, created_at=datetime.now(timezone.utc),
            ),
            account_links=None, message_search=None,
            hibernation_queries=self.queries,
        )

    async def test_status_read_requires_log_access_and_excludes_private_fields(self):
        result = await read_active_hibernation(self.context, {})
        self.assertEqual(result["active_record_count"], 2)
        self.assertEqual(result["missing_start_time_count"], 1)
        self.assertEqual(result["records"][0]["member_id"], 42)
        self.assertIn("newest recorded start first", result["record_order"])
        self.assertIn("without a start time last", result["record_order"])
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

    async def test_status_read_limit_covers_more_records_within_bounds(self):
        self.assertEqual(len((await read_active_hibernation(self.context, {"limit": 1}))["records"]), 1)
        self.assertIn("error", await read_active_hibernation(self.context, {"limit": 501}))

    async def test_255_records_keep_the_oldest_ten_within_the_model_result_budget(self):
        from elbow_helper.features.agent.engine.budgets import MAX_TOOL_RESULT_CHARACTERS
        from elbow_helper.features.agent.engine.tool_call import bound_tool_result
        from elbow_helper.features.agent.plan.results import model_result, model_view

        records = tuple(ActiveHibernationRecord(
            1000000000000000000 + index,
            datetime.fromtimestamp(1750000000 - index * 86400, timezone.utc).isoformat(),
            1750000000 - index * 86400, "recorded",
        ) for index in range(254)) + (ActiveHibernationRecord(2000000000000000000, None, None, "unavailable"),)
        self.queries.active_snapshot.return_value = ActiveHibernationSnapshot(
            "2026-10-21T12:00:00+00:00", 255, 0, 0, 1, records,
        )
        payload = await read_active_hibernation(self.context, {"limit": 500})
        raw = json.dumps(payload, separators=(",", ":"))
        view = model_view(model_result(payload))
        content = json.dumps(view, separators=(",", ":"))
        bounded = json.loads(bound_tool_result(content, MAX_TOOL_RESULT_CHARACTERS))
        self.assertGreater(len(raw), MAX_TOOL_RESULT_CHARACTERS)
        self.assertLess(len(content), MAX_TOOL_RESULT_CHARACTERS)
        self.assertFalse(bounded["flags"]["truncated"])
        self.assertNotIn("truncated", bounded)
        self.assertEqual(bounded["records"]["columns"], ["member_id", "started_ts"])
        rows = [dict(zip(bounded["records"]["columns"], row)) for row in bounded["records"]["rows"]]
        self.assertEqual(len(rows), 255)
        oldest = sorted((row for row in rows if row["started_ts"] is not None), key=lambda row: row["started_ts"])[:10]
        self.assertEqual([row["member_id"] for row in oldest], [row.member_id for row in reversed(records[244:254])])
        self.assertIsNone(rows[-1]["started_ts"])
        self.assertEqual(payload["records"], [asdict(row) for row in records])
        self.assertEqual(self.context.state.reports[payload["report_id"]].snapshot.records, records)
        # A genuinely smaller allowance still reports omitted rows explicitly.
        small = json.loads(bound_tool_result(content, 2500))
        self.assertTrue(small["truncated"])
        self.assertLess(len(small["records"]["rows"]), 255)

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
        self.context.state.reports["role"] = make_event_report("role", "now")
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
