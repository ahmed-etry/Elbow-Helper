from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from elbow_helper.data.events.testing import MemoryEventsStore
from elbow_helper.features.member_lifecycle import reports
from elbow_helper.features.member_lifecycle.config import MAX_OVERDUE_APPLICANTS_DISPLAY
from elbow_helper.features.member_lifecycle.reports import ReportsMixin
from elbow_helper.features.member_lifecycle.state import LifecycleState
from elbow_helper.features.member_lifecycle.views import ApplicantCleanupSelectView, ApplicantCleanupView


class Owner(ReportsMixin):
    @property
    def state(self):
        return self.lifecycle_state.state


class MemberLifecycleReportTests(unittest.TestCase):
    def test_applicant_scan_checks_daily_for_newly_overdue_applicants(self) -> None:
        self.assertEqual(ReportsMixin.applicant_linger_scan.hours, 24.0)


    def test_applicant_report_limits_cards_and_explains_hidden_results(self) -> None:
        now = datetime(2026, 9, 12, tzinfo=timezone.utc)
        applicants = [
            (
                SimpleNamespace(
                    id=index,
                    display_name=f"Applicant {index}",
                    mention=f"<@{index}>",
                ),
                now,
                14,
            )
            for index in range(MAX_OVERDUE_APPLICANTS_DISPLAY + 2)
        ]

        embed = ReportsMixin._build_applicant_linger_embed(applicants, {}, now)

        self.assertIn(f"**{MAX_OVERDUE_APPLICANTS_DISPLAY}.**", embed.description)
        self.assertNotIn(f"**{MAX_OVERDUE_APPLICANTS_DISPLAY + 1}.**", embed.description)
        self.assertEqual(
            embed.footer.text,
            f"Showing {MAX_OVERDUE_APPLICANTS_DISPLAY} of {len(applicants)} applicants.",
        )


class MemberLifecycleViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_scan_posts_once_when_an_applicant_becomes_overdue(self) -> None:
        applicant_role = object()
        member = SimpleNamespace(
            id=1,
            display_name="Applicant",
            mention="<@1>",
            roles=[applicant_role],
            joined_at=datetime.now(timezone.utc) - timedelta(days=14, minutes=1),
        )
        message = SimpleNamespace(id=42, edit=AsyncMock())

        class FakeTextChannel:
            def __init__(self) -> None:
                self.send = AsyncMock(return_value=message)

        channel = FakeTextChannel()
        guild = SimpleNamespace(
            members=[member],
            get_channel=lambda _channel_id: channel,
            get_role=lambda _role_id: applicant_role,
        )
        events = MemoryEventsStore()
        self.addCleanup(events.close)
        cog = Owner()
        cog.lifecycle_state = LifecycleState(events)
        await cog.lifecycle_state.startup()
        cog.bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            get_guild=lambda _guild_id: guild,
        )
        cog._refresh_ticket_log_index = AsyncMock()

        with patch.object(reports.discord, "TextChannel", FakeTextChannel):
            await ReportsMixin.applicant_linger_scan.coro(cog)
            await ReportsMixin.applicant_linger_scan.coro(cog)

        channel.send.assert_awaited_once()
        self.assertEqual(cog.state["overdue_applicant_ids"], [member.id])
        self.assertEqual(cog.state["applicant_reports"]["42"]["applicant_ids"], [member.id])
        restarted = LifecycleState(events)
        await restarted.startup()
        self.assertEqual(restarted.state, cog.state)

    async def test_review_offers_only_current_applicants_and_reports_close_when_none_remain(self) -> None:
        # A 31 Oct report still offered a member who had since been accepted.
        from elbow_helper.features.member_lifecycle import views
        applicant_role = object()
        accepted = SimpleNamespace(id=1, display_name="Accepted", roles=[], joined_at=None)
        waiting = SimpleNamespace(id=2, display_name="Waiting", roles=[applicant_role],
                                  joined_at=datetime.now(timezone.utc))
        class FakeTextChannel:
            send = AsyncMock()
            fetch_message = AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))

        channel = FakeTextChannel()
        guild = SimpleNamespace(members=[accepted, waiting], get_channel=lambda _channel_id: channel,
                                get_role=lambda _role_id: applicant_role,
                                get_member={1: accepted, 2: waiting}.get)
        events = MemoryEventsStore()
        self.addCleanup(events.close)
        cog = Owner()
        cog.lifecycle_state = LifecycleState(events)
        await cog.lifecycle_state.startup()
        await cog.lifecycle_state.change(lambda connection, state: state["applicant_reports"].update(
            {"42": {"applicant_ids": [1, 2], "created_iso": "x", "active": True}}) or ())
        cog.bot = SimpleNamespace(wait_until_ready=AsyncMock(), get_guild=lambda _guild_id: guild)
        cog._refresh_ticket_log_index = AsyncMock()
        interaction = SimpleNamespace(user=SimpleNamespace(guild_permissions=SimpleNamespace(kick_members=True)),
                                      guild=guild, response=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(views, "send_bound_view", AsyncMock()) as sent:
            await ApplicantCleanupView(cog, 42).cleanup(interaction)
        self.assertEqual(sent.await_args.kwargs["view"].applicants, [(2, "Waiting")])

        waiting.roles = []
        with patch.object(reports.discord, "TextChannel", FakeTextChannel):
            await ReportsMixin.applicant_linger_scan.coro(cog)
        self.assertFalse(cog.state["applicant_reports"]["42"]["active"])
        channel.fetch_message.assert_awaited_once_with(42)

    async def test_cleanup_ui_presents_a_review_before_removal(self) -> None:
        report_view = ApplicantCleanupView(None, 42)
        review_button = report_view.children[0]
        self.assertEqual(review_button.label, "Review Applicants")

        selection_view = ApplicantCleanupSelectView(
            None,
            42,
            [(1, "First Applicant"), (2, "Second Applicant")],
        )
        self.assertEqual(selection_view.select.placeholder, "Choose applicants to remove")
        self.assertEqual(selection_view.confirm_button.label, "Remove Selected (2)")
        self.assertEqual(
            selection_view.content(),
            "Choose which applicants to remove. Everyone is selected by default; "
            "deselect anyone to keep.\nPage 1/1 • Selected: 2",
        )


if __name__ == "__main__":
    unittest.main()
