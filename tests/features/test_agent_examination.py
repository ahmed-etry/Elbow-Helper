from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from elbow_helper.configuration.channels import (
    EXAMINATION_ROOM,
    EXAMINATION_TICKET_CATEGORY,
)
from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.reports.examination import ExaminationCaseReport
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.reports.roles import RoleAccountReport
from elbow_helper.features.agent.tools.examination import (
    read_accessible_examination_cases,
    read_examination_case_report,
)
from elbow_helper.features.examination.queries import ExaminationQueries


def _case(channel_id, applicant_id, **changes):
    value = {
        "ticket_channel_id": channel_id,
        "type": "elder_promo",
        "opener_id": applicant_id,
        "exam_required": True,
        "routing_inflight": False,
        "routing_message_id": 900 + channel_id,
        "stage": "initial",
        "responded": False,
        "availability": "private availability",
        "elder_reason": "private answer",
        "pinged_ids": [88],
    }
    value.update(changes)
    return value


class _Channel:
    def __init__(self, channel_id, *, allowed=True, category_id=None):
        self.id = channel_id
        self.allowed = allowed
        self.denied_actor_ids = set()
        self.category_id = (
            EXAMINATION_TICKET_CATEGORY if category_id is None else category_id
        )
        self.type = discord.ChannelType.text
        self.guild = None

    def permissions_for(self, actor):
        allowed = self.allowed and actor.id not in self.denied_actor_ids
        return SimpleNamespace(
            view_channel=allowed,
            read_message_history=allowed,
        )


class AgentExaminationToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        requester = SimpleNamespace(
            id=10, roles=[SimpleNamespace(id=next(iter(CORE)))],
        )
        bot_member = SimpleNamespace(id=20)
        self.requester = requester
        self.bot_member = bot_member
        self.source = _Channel(100)
        self.routing = _Channel(EXAMINATION_ROOM)
        self.first = _Channel(200)
        self.second = _Channel(300)
        self.hidden = _Channel(400, allowed=False)
        channels = {
            row.id: row
            for row in (
                self.source, self.routing, self.first, self.second, self.hidden,
            )
        }
        guild = SimpleNamespace(
            id=1,
            me=bot_member,
            get_member=lambda value: requester if value == requester.id else None,
            get_channel_or_thread=lambda value: channels.get(value),
        )
        for channel in channels.values():
            channel.guild = guild
        self.state = {
            "200": _case(
                200, 42, type="clan_promo", intake_state="selecting_from",
                exam_required=None, routing_message_id=None, stage="pending",
            ),
            "300": _case(300, 84, responded=True),
            "400": _case(
                400, 126, elder_reason="hidden answer", availability="hidden time",
            ),
        }
        real_queries = ExaminationQueries(
            lambda: self.state,
            clock=lambda: datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
        )
        self.queries = SimpleNamespace(
            case_registrations=MagicMock(
                side_effect=real_queries.case_registrations,
            ),
            case_snapshot=MagicMock(side_effect=real_queries.case_snapshot),
        )
        self.context = AgentRequestContext(
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)),
            guild=guild,
            member=requester,
            source_message=SimpleNamespace(
                channel=self.source,
                created_at=datetime.now(timezone.utc),
            ),
            account_links=None,
            clan_health=None,
            message_search=None,
            examination_queries=self.queries,
        )

    async def test_read_authorizes_before_projection_and_retains_status_only(self):
        result = await read_accessible_examination_cases(self.context, {})

        self.assertEqual(result["accessible_selected_entry_count"], 2)
        self.assertEqual(result["accessible_case_count"], 2)
        self.assertNotIn("stored_entry_count", result)
        self.assertNotIn("omitted_unselected_count", result)
        self.queries.case_snapshot.assert_called_once_with(
            ticket_channel_ids=(200, 300),
        )
        self.assertEqual(
            self.context.state.source_channels,
            {EXAMINATION_ROOM, 200, 300},
        )
        self.assertNotIn(
            126, [row["applicant_member_id"] for row in result["cases"]],
        )
        for row in result["cases"]:
            self.assertNotIn("availability", row)
            self.assertNotIn("elder_reason", row)
            self.assertNotIn("pinged_ids", row)
            self.assertNotIn("routing_message_id", row)
        self.assertFalse(result["message_history_read"])
        report = self.context.state.reports[result["report_id"]]
        self.assertIsInstance(report, ExaminationCaseReport)

        filtered = await read_examination_case_report(self.context, {
            "report_id": result["report_id"],
            "applicant_member_id": 84,
            "case_type": "elder_promo",
            "workflow_status": "response_recorded",
            "response_status": "recorded",
        })
        self.assertEqual(filtered["matched_count"], 1)
        self.assertEqual(filtered["cases"][0]["ticket_channel_id"], 300)
        self.assertEqual(self.queries.case_snapshot.call_count, 1)

    async def test_empty_accessible_status_has_no_hidden_counts_or_artifact(self):
        self.first.allowed = False
        self.second.allowed = False
        result = await read_accessible_examination_cases(self.context, {})
        self.assertEqual(result["accessible_selected_entry_count"], 0)
        self.assertEqual(result["accessible_case_count"], 0)
        self.assertNotIn("omitted", str(result))
        self.assertIsNone(result["report_id"])
        self.assertEqual(self.context.state.reports, {})
        self.assertEqual(self.context.state.source_channels, {EXAMINATION_ROOM})

    async def test_malformed_snapshot_does_not_retain_candidate_sources(self):
        self.queries.case_snapshot.side_effect = ValueError("malformed")
        result = await read_accessible_examination_cases(self.context, {})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.source_channels, set())
        self.assertEqual(self.context.state.reports, {})

    async def test_routing_room_access_is_required_before_case_state_read(self):
        for actor_id in (self.requester.id, self.bot_member.id):
            with self.subTest(actor_id=actor_id):
                self.routing.denied_actor_ids.add(actor_id)
                result = await read_accessible_examination_cases(self.context, {})
                self.assertIn("error", result)
                self.queries.case_registrations.assert_not_called()
                self.assertEqual(self.context.state.source_channels, set())
                self.routing.denied_actor_ids.clear()

    async def test_access_revoked_during_projection_prevents_result_and_retention(self):
        project = self.queries.case_snapshot.side_effect

        def revoke_after_projection(*, ticket_channel_ids):
            snapshot = project(ticket_channel_ids=ticket_channel_ids)
            self.first.denied_actor_ids.add(self.requester.id)
            return snapshot

        self.queries.case_snapshot.side_effect = revoke_after_projection
        with self.assertRaises(AgentAccessLost):
            await read_accessible_examination_cases(self.context, {})
        self.assertEqual(self.context.state.reports, {})

    async def test_snapshot_cannot_substitute_an_unauthorized_ticket(self):
        malicious = ExaminationQueries(
            lambda: self.state,
            clock=lambda: datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
        ).case_snapshot(ticket_channel_ids=(400,))
        self.queries.case_snapshot.side_effect = None
        self.queries.case_snapshot.return_value = malicious
        result = await read_accessible_examination_cases(self.context, {})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.source_channels, set())

    async def test_invalid_accessible_record_remains_a_revocable_source(self):
        self.state["200"]["intake_state"] = "invalid"
        result = await read_accessible_examination_cases(self.context, {})
        self.assertEqual(result["skipped_invalid_accessible_count"], 1)
        self.assertEqual(
            self.context.state.source_channels,
            {EXAMINATION_ROOM, 200, 300},
        )

        self.first.allowed = False
        with self.assertRaises(AgentAccessLost):
            await read_examination_case_report(
                self.context, {"report_id": result["report_id"]},
            )

    async def test_report_reuse_rechecks_requester_bot_and_core_access(self):
        result = await read_accessible_examination_cases(self.context, {})
        report_id = result["report_id"]

        self.first.denied_actor_ids.add(self.requester.id)
        with self.assertRaises(AgentAccessLost):
            await read_examination_case_report(
                self.context, {"report_id": report_id},
            )
        self.first.denied_actor_ids.clear()

        self.second.denied_actor_ids.add(self.bot_member.id)
        with self.assertRaises(AgentAccessLost):
            await read_examination_case_report(
                self.context, {"report_id": report_id},
            )
        self.second.denied_actor_ids.clear()

        self.routing.denied_actor_ids.add(self.requester.id)
        with self.assertRaises(AgentAccessLost):
            await read_examination_case_report(
                self.context, {"report_id": report_id},
            )
        self.routing.denied_actor_ids.clear()

        original_roles = self.requester.roles
        self.requester.roles = []
        try:
            with self.assertRaises(AgentAccessLost):
                await read_examination_case_report(
                    self.context, {"report_id": report_id},
                )
        finally:
            self.requester.roles = original_roles

    async def test_wrong_category_is_not_projected(self):
        self.second.category_id = 999
        result = await read_accessible_examination_cases(self.context, {})
        self.assertEqual(result["accessible_selected_entry_count"], 1)
        self.assertEqual(result["accessible_case_count"], 1)
        self.assertEqual(
            self.context.state.source_channels,
            {EXAMINATION_ROOM, 200},
        )

    async def test_budget_wrong_kind_foreign_guild_and_filters_fail_closed(self):
        with patch("elbow_helper.features.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES", 1):
            result = await read_accessible_examination_cases(self.context, {})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.reports, {})

        self.context.state.source_channels.clear()
        result = await read_accessible_examination_cases(self.context, {})
        report = self.context.state.reports[result["report_id"]]
        self.context.state.reports["role"] = RoleAccountReport(
            "role", "now", (), (),
        )
        self.context.state.reports["foreign"] = ExaminationCaseReport(
            "foreign", 2, report.snapshot,
        )
        for report_id in ("missing", "role", "foreign"):
            value = await read_examination_case_report(
                self.context, {"report_id": report_id},
            )
            self.assertIn("error", value)
        for invalid in (
            {"ticket_channel_id": True},
            {"applicant_member_id": 0},
            {"case_type": "support"},
            {"workflow_status": "approved"},
            {"response_status": "answered"},
        ):
            value = await read_examination_case_report(
                self.context,
                {"report_id": result["report_id"], **invalid},
            )
            self.assertIn("error", value)


if __name__ == "__main__":
    unittest.main()
