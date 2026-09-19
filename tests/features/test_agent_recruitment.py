from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from elbow_helper.configuration.channels import RECRUITMENT_TICKET_CATEGORY
from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.reports.recruitment import RecruitmentTrialReport
from elbow_helper.features.agent.reports.roles import RoleAccountReport
from elbow_helper.features.agent.tools.recruitment import (
    read_active_recruitment_trial_report,
    read_active_recruitment_trials,
)
from elbow_helper.features.recruitment.queries import RecruitmentQueries


class _Channel:
    def __init__(self, channel_id, *, allowed=True, category_id=None):
        self.id = channel_id
        self.allowed = allowed
        self.denied_actor_ids = set()
        self.category_id = (
            RECRUITMENT_TICKET_CATEGORY if category_id is None else category_id
        )
        self.type = discord.ChannelType.text
        self.guild = None

    def permissions_for(self, actor):
        allowed = self.allowed and actor.id not in self.denied_actor_ids
        return SimpleNamespace(
            view_channel=allowed,
            read_message_history=allowed,
        )


class AgentRecruitmentToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        requester = SimpleNamespace(
            id=10, roles=[SimpleNamespace(id=next(iter(CORE)))],
        )
        bot_member = SimpleNamespace(id=20)
        self.requester = requester
        self.bot_member = bot_member
        self.source = _Channel(100)
        self.first = _Channel(200)
        self.second = _Channel(300)
        self.hidden = _Channel(400, allowed=False)
        channels = {
            row.id: row
            for row in (self.source, self.first, self.second, self.hidden)
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
            "200": {
                "start": "2026-09-16T12:00:00+00:00",
                "days": 2,
                "applicant_id": 42,
                "tracking_msg_id": 999,
                "private_note": "first private note",
            },
            "300": {
                "start": "2026-09-10T12:00:00+00:00",
                "days": 3,
                "applicant_id": 84,
            },
            "400": {
                "start": "2026-09-15T12:00:00+00:00",
                "days": 7,
                "applicant_id": 126,
                "private_note": "hidden private note",
            },
        }
        real_queries = RecruitmentQueries(
            lambda: self.state,
            clock=lambda: datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
        )
        self.queries = SimpleNamespace(
            active_trial_registrations=MagicMock(
                side_effect=real_queries.active_trial_registrations,
            ),
            active_trial_snapshot=MagicMock(
                side_effect=real_queries.active_trial_snapshot,
            ),
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
            recruitment_queries=self.queries,
        )

    async def test_read_authorizes_before_status_projection_and_retains_all_rows(self):
        result = await read_active_recruitment_trials(self.context, {})

        self.assertEqual(result["accessible_selected_entry_count"], 2)
        self.assertEqual(result["accessible_active_trial_count"], 2)
        self.assertNotIn("omitted_unselected_count", result)
        self.assertNotIn("stored_entry_count", result)
        self.queries.active_trial_snapshot.assert_called_once_with(
            ticket_channel_ids=(200, 300),
        )
        self.assertEqual(self.context.state.source_channels, {200, 300})
        self.assertNotIn(
            126, [row["applicant_member_id"] for row in result["trials"]],
        )
        for row in result["trials"]:
            self.assertNotIn("tracking_msg_id", row)
            self.assertNotIn("private_note", row)
        self.assertFalse(result["message_history_read"])
        report = self.context.state.reports[result["report_id"]]
        self.assertIsInstance(report, RecruitmentTrialReport)

        filtered = await read_active_recruitment_trial_report(self.context, {
            "report_id": result["report_id"],
            "applicant_member_id": 84,
            "timing_status": "due",
        })
        self.assertEqual(filtered["matched_count"], 1)
        self.assertEqual(filtered["trials"][0]["ticket_channel_id"], 300)
        self.assertEqual(self.queries.active_trial_snapshot.call_count, 1)

    async def test_empty_accessible_status_returns_coverage_without_artifact(self):
        self.first.allowed = False
        self.second.allowed = False
        result = await read_active_recruitment_trials(self.context, {})
        self.assertEqual(result["accessible_active_trial_count"], 0)
        self.assertEqual(result["accessible_selected_entry_count"], 0)
        self.assertNotIn("omitted_unselected_count", result)
        self.assertIsNone(result["report_id"])
        self.assertEqual(self.context.state.reports, {})
        self.assertEqual(self.context.state.source_channels, set())

    async def test_malformed_snapshot_does_not_retain_candidate_sources(self):
        self.queries.active_trial_snapshot.side_effect = ValueError("malformed")
        result = await read_active_recruitment_trials(self.context, {})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.source_channels, set())
        self.assertEqual(self.context.state.reports, {})

    async def test_access_revoked_during_projection_prevents_result_and_retention(self):
        project = self.queries.active_trial_snapshot.side_effect

        def revoke_after_projection(*, ticket_channel_ids):
            snapshot = project(ticket_channel_ids=ticket_channel_ids)
            self.first.denied_actor_ids.add(self.requester.id)
            return snapshot

        self.queries.active_trial_snapshot.side_effect = revoke_after_projection
        with self.assertRaises(AgentAccessLost):
            await read_active_recruitment_trials(self.context, {})
        self.assertEqual(self.context.state.reports, {})

    async def test_snapshot_cannot_substitute_an_unauthorized_ticket(self):
        malicious = RecruitmentQueries(
            lambda: self.state,
            clock=lambda: datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
        ).active_trial_snapshot(ticket_channel_ids=(400,))
        self.queries.active_trial_snapshot.side_effect = None
        self.queries.active_trial_snapshot.return_value = malicious
        result = await read_active_recruitment_trials(self.context, {})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.source_channels, set())

    async def test_invalid_accessible_record_remains_a_revocable_source(self):
        self.state["200"]["start"] = "bad"
        result = await read_active_recruitment_trials(self.context, {})
        self.assertEqual(result["skipped_invalid_accessible_count"], 1)
        self.assertEqual(self.context.state.source_channels, {200, 300})

        self.first.allowed = False
        with self.assertRaises(AgentAccessLost):
            await read_active_recruitment_trial_report(
                self.context, {"report_id": result["report_id"]},
            )

    async def test_report_reuse_rechecks_every_ticket_source(self):
        result = await read_active_recruitment_trials(self.context, {})
        self.first.allowed = False
        with self.assertRaises(AgentAccessLost):
            await read_active_recruitment_trial_report(
                self.context, {"report_id": result["report_id"]},
            )

    async def test_report_reuse_fails_after_requester_bot_or_core_access_changes(self):
        result = await read_active_recruitment_trials(self.context, {})
        report_id = result["report_id"]

        self.first.denied_actor_ids.add(self.requester.id)
        with self.assertRaises(AgentAccessLost):
            await read_active_recruitment_trial_report(
                self.context, {"report_id": report_id},
            )
        self.first.denied_actor_ids.clear()

        self.second.denied_actor_ids.add(self.bot_member.id)
        with self.assertRaises(AgentAccessLost):
            await read_active_recruitment_trial_report(
                self.context, {"report_id": report_id},
            )
        self.second.denied_actor_ids.clear()

        original_roles = self.requester.roles
        self.requester.roles = []
        try:
            with self.assertRaises(AgentAccessLost):
                await read_active_recruitment_trial_report(
                    self.context, {"report_id": report_id},
                )
        finally:
            self.requester.roles = original_roles

    async def test_wrong_category_is_not_projected(self):
        self.second.category_id = 999
        result = await read_active_recruitment_trials(self.context, {})
        self.assertEqual(result["accessible_active_trial_count"], 1)
        self.assertEqual(result["accessible_selected_entry_count"], 1)
        self.assertEqual(self.context.state.source_channels, {200})

    async def test_budget_wrong_kind_foreign_guild_and_filters_fail_closed(self):
        with patch("elbow_helper.features.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES", 1):
            result = await read_active_recruitment_trials(self.context, {})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.reports, {})

        self.context.state.source_channels.clear()
        result = await read_active_recruitment_trials(self.context, {})
        report = self.context.state.reports[result["report_id"]]
        self.context.state.reports["role"] = RoleAccountReport(
            "role", "now", (), (),
        )
        self.context.state.reports["foreign"] = RecruitmentTrialReport(
            "foreign", 2, report.snapshot,
        )
        for report_id in ("missing", "role", "foreign"):
            value = await read_active_recruitment_trial_report(
                self.context, {"report_id": report_id},
            )
            self.assertIn("error", value)
        for invalid in (
            {"ticket_channel_id": True},
            {"applicant_member_id": 0},
            {"timing_status": "accepted"},
        ):
            value = await read_active_recruitment_trial_report(
                self.context,
                {"report_id": result["report_id"], **invalid},
            )
            self.assertIn("error", value)


if __name__ == "__main__":
    unittest.main()
