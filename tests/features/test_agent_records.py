from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.configuration.roles import CORE, LEAD_PLUS
from elbow_helper.features.agent.access import ACCESS_LEAD_PLUS, AgentAccessLost
from elbow_helper.features.agent.reports.leadership_record import (
    LeadershipRecordReport,
)
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.reports.roles import RoleAccountReport
from elbow_helper.features.agent.tools.records import (
    read_active_leadership_records,
    read_leadership_record_report,
)
from elbow_helper.features.records.queries import LeadershipRecordQueries


def _record(record_id=2, member_id=42, **changes):
    value = {
        "id": record_id,
        "created_ts": 1_700_000_000 + record_id,
        "updated_ts": 1_700_000_100 + record_id,
        "status": "active",
        "member_id": member_id,
        "member_display": "Member",
        "category_key": "war",
        "incident_type_key": "war_missed_attacks",
        "note": "Missed both attacks.",
        "recorder_id": 99,
        "recorder_display": "Lead",
        "edited_by_id": None,
        "removed_ts": None,
    }
    value.update(changes)
    return value


class _Channel:
    def __init__(self, channel_id=100):
        self.id = channel_id
        self.denied_actor_ids = set()
        self.guild = None

    def permissions_for(self, actor):
        allowed = actor.id not in self.denied_actor_ids
        return SimpleNamespace(
            view_channel=allowed,
            read_message_history=allowed,
        )


class AgentLeadershipRecordToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requester = SimpleNamespace(
            id=10,
            roles=[
                SimpleNamespace(id=next(iter(CORE))),
                SimpleNamespace(id=next(iter(LEAD_PLUS))),
            ],
        )
        self.bot_member = SimpleNamespace(id=20)
        self.channel = _Channel()
        guild = SimpleNamespace(
            id=1,
            me=self.bot_member,
            get_member=lambda value: (
                self.requester if value == self.requester.id else None
            ),
            get_channel_or_thread=lambda value: (
                self.channel if value == self.channel.id else None
            ),
        )
        self.channel.guild = guild
        self.reader = MagicMock()
        self.reader.list.return_value = [
            _record(),
            _record(
                1, 84, member_display="Other",
                category_key="communication",
                incident_type_key="communication_no_response",
                note="No response after two follow-ups.",
            ),
        ]
        real_queries = LeadershipRecordQueries(
            self.reader,
            clock=lambda: datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
        )
        self.queries = SimpleNamespace(
            active_snapshot=MagicMock(side_effect=real_queries.active_snapshot),
        )
        self.context = AgentRequestContext(
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)),
            guild=guild,
            member=self.requester,
            source_message=SimpleNamespace(
                channel=self.channel,
                created_at=datetime.now(timezone.utc),
            ),
            account_links=None,
            clan_health=None,
            message_search=None,
            record_queries=self.queries,
        )
        self.context.state.source_channels.add(self.channel.id)

    async def test_read_requires_lead_plus_and_retains_complete_active_notes(self):
        result = await read_active_leadership_records(self.context, {})

        self.assertEqual(result["active_record_count"], 2)
        self.assertEqual(result["records"][0]["note"], "Missed both attacks.")
        self.assertNotIn("removed", str(result["records"]))
        self.assertNotIn("recorder_id", str(result["records"]))
        self.assertEqual(
            self.context.state.required_access, {ACCESS_LEAD_PLUS},
        )
        report = self.context.state.reports[result["report_id"]]
        self.assertIsInstance(report, LeadershipRecordReport)

        filtered = await read_leadership_record_report(self.context, {
            "report_id": result["report_id"],
            "category_key": "communication",
            "incident_type_key": "communication_no_response",
            "search": "two follow-ups",
        })
        self.assertEqual(filtered["matched_count"], 1)
        self.assertEqual(filtered["records"][0]["member_id"], 84)
        self.assertEqual(self.queries.active_snapshot.call_count, 1)

    async def test_member_scope_is_forwarded_without_loading_other_records(self):
        self.reader.list.return_value = [_record(member_id=42)]
        result = await read_active_leadership_records(
            self.context, {"member_id": 42},
        )
        self.assertEqual(result["member_id_filter"], 42)
        self.queries.active_snapshot.assert_called_once_with(member_id=42)
        self.assertEqual([row["member_id"] for row in result["records"]], [42])

    async def test_missing_lead_plus_fails_before_private_read(self):
        self.requester.roles = [SimpleNamespace(id=next(iter(CORE)))]
        with self.assertRaises(AgentAccessLost):
            await read_active_leadership_records(self.context, {})
        self.queries.active_snapshot.assert_not_called()
        self.assertEqual(self.context.state.required_access, set())
        self.assertEqual(self.context.state.reports, {})

    async def test_role_revoked_during_read_prevents_result_and_retention(self):
        project = self.queries.active_snapshot.side_effect

        def revoke_after_read(*, member_id=None):
            snapshot = project(member_id=member_id)
            self.requester.roles = [SimpleNamespace(id=next(iter(CORE)))]
            return snapshot

        self.queries.active_snapshot.side_effect = revoke_after_read
        with self.assertRaises(AgentAccessLost):
            await read_active_leadership_records(self.context, {})
        self.assertEqual(self.context.state.required_access, set())
        self.assertEqual(self.context.state.reports, {})

    async def test_report_reuse_rechecks_role_core_and_source_access(self):
        result = await read_active_leadership_records(self.context, {})
        report_id = result["report_id"]

        original_roles = self.requester.roles
        self.requester.roles = [SimpleNamespace(id=next(iter(CORE)))]
        with self.assertRaises(AgentAccessLost):
            await read_leadership_record_report(
                self.context, {"report_id": report_id},
            )
        self.requester.roles = original_roles

        self.requester.roles = [SimpleNamespace(id=next(iter(LEAD_PLUS)))]
        with self.assertRaises(AgentAccessLost):
            await read_leadership_record_report(
                self.context, {"report_id": report_id},
            )
        self.requester.roles = original_roles

        self.channel.denied_actor_ids.add(self.bot_member.id)
        with self.assertRaises(AgentAccessLost):
            await read_leadership_record_report(
                self.context, {"report_id": report_id},
            )

    async def test_empty_malformed_oversized_and_wrong_reports_fail_closed(self):
        self.reader.list.return_value = []
        empty = await read_active_leadership_records(self.context, {})
        self.assertEqual(empty["active_record_count"], 0)
        self.assertIsNone(empty["report_id"])
        self.assertEqual(self.context.state.reports, {})
        self.assertEqual(self.context.state.required_access, {ACCESS_LEAD_PLUS})

        self.context.state.required_access.clear()
        self.queries.active_snapshot.side_effect = ValueError("malformed")
        malformed = await read_active_leadership_records(self.context, {})
        self.assertIn("error", malformed)
        self.assertEqual(self.context.state.required_access, set())

        self.queries.active_snapshot.side_effect = LeadershipRecordQueries(
            MagicMock(list=MagicMock(return_value=[_record()])),
            clock=lambda: datetime.now(timezone.utc),
        ).active_snapshot
        with patch("elbow_helper.features.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES", 1):
            oversized = await read_active_leadership_records(self.context, {})
        self.assertIn("error", oversized)
        self.assertEqual(self.context.state.reports, {})

        self.context.state.reports["role"] = RoleAccountReport(
            "role", "now", (), (),
        )
        for report_id in ("missing", "role"):
            value = await read_leadership_record_report(
                self.context, {"report_id": report_id},
            )
            self.assertIn("error", value)

    async def test_filters_validate_without_disclosing_report_data(self):
        result = await read_active_leadership_records(self.context, {})
        report_id = result["report_id"]
        for arguments in (
            {"member_id": True},
            {"category_key": "private"},
            {"incident_type_key": "unknown"},
            {"search": " "},
            {"search": "x" * 201},
        ):
            value = await read_leadership_record_report(
                self.context, {"report_id": report_id, **arguments},
            )
            self.assertIn("error", value)


if __name__ == "__main__":
    unittest.main()
