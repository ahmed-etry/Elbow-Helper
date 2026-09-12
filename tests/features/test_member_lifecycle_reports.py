from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from elbow_helper.features.member_lifecycle import reports
from elbow_helper.features.member_lifecycle.config import MAX_OVERDUE_APPLICANTS_DISPLAY
from elbow_helper.features.member_lifecycle.reports import ReportsMixin
from elbow_helper.features.member_lifecycle.views import ApplicantCleanupSelectView, ApplicantCleanupView


class MemberLifecycleReportTests(unittest.TestCase):
    def test_applicant_scan_checks_daily_for_newly_overdue_applicants(self) -> None:
        self.assertEqual(ReportsMixin.applicant_linger_scan.hours, 24.0)

    def test_applicant_report_uses_readable_ages_and_ticket_log_actions(self) -> None:
        now = datetime(2026, 9, 12, tzinfo=timezone.utc)
        linked = SimpleNamespace(id=1, display_name="Linked Applicant", mention="<@1>")
        unlinked = SimpleNamespace(id=2, display_name="Unlinked Applicant", mention="<@2>")

        embed = ReportsMixin._build_applicant_linger_embed(
            [
                (linked, now, 14),
                (unlinked, now, 15),
            ],
            {"1": ["https://discord.com/channels/1/2/3"]},
            now,
        )

        self.assertEqual(
            embed.description,
            "**1.** <@1>\n"
            "Joined **14 days ago** · [Open ticket log](https://discord.com/channels/1/2/3)\n\n"
            "**2.** <@2>\n"
            "Joined **15 days ago** · No ticket log found",
        )

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
        cog = object.__new__(ReportsMixin)
        cog.bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            get_guild=lambda _guild_id: guild,
        )
        cog.state = {
            "overdue_applicant_ids": [],
            "ticket_owner_links": {},
            "applicant_reports": {},
        }
        cog._refresh_ticket_log_index = AsyncMock()

        with (
            patch.object(reports.discord, "TextChannel", FakeTextChannel),
            patch.object(reports, "save_state"),
        ):
            await ReportsMixin.applicant_linger_scan.coro(cog)
            await ReportsMixin.applicant_linger_scan.coro(cog)

        channel.send.assert_awaited_once()
        self.assertEqual(cog.state["overdue_applicant_ids"], [member.id])

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
