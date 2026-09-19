from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.reports.movement import FamilyMovementReport
from elbow_helper.features.agent.reports.roles import RoleAccountReport
from elbow_helper.features.agent.tools.clan_health import (
    read_family_account_movement_report,
    read_family_account_movements,
)
from elbow_helper.features.clan_health.queries import (
    CompleteFamilySnapshotRun,
    FamilyAccountMovement,
    FamilyMovementHistory,
    FamilySnapshotInterval,
)


def _history(*, movements=True):
    old = CompleteFamilySnapshotRun("old", 100, 2, 2, ())
    new = CompleteFamilySnapshotRun("new", 200, 2, 2, ())
    rows = (
        FamilyAccountMovement(
            "#P0", "Alpha", "observed_family_clan_change", "BEH", "BEC",
            "old", 100, "new", 200,
        ),
        FamilyAccountMovement(
            "#P2", "Beta", "observed_left_family", "BEH", None,
            "old", 100, "new", 200,
        ),
        FamilyAccountMovement(
            "#P8", "Gamma", "observed_entered_family", None, "BEC",
            "old", 100, "new", 200,
        ),
    ) if movements else ()
    interval = FamilySnapshotInterval(
        "old", 100, "new", 200, 2, 2, len(rows), (),
    )
    return FamilyMovementHistory(
        "2026-09-17T00:00:00+00:00", (new, old), (interval,), rows, None,
    )


class _Links:
    def __init__(self):
        self.calls = 0

    def get_links_by_tags(self, tags):
        self.calls += 1
        return {
            tag: ({"discord_user_id": 42, "player_name_last_seen": "Owner"}
                  if tag in {"#P0", "#P2"} else None)
            for tag in tags
        }


class AgentMovementToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        member = SimpleNamespace(id=42, roles=[SimpleNamespace(id=next(iter(CORE)))])
        guild = SimpleNamespace(id=1, me=member, get_member=lambda _: member)
        channel = SimpleNamespace(
            id=100, guild=guild,
            permissions_for=lambda _: SimpleNamespace(
                view_channel=True, read_message_history=True,
            ),
        )
        guild.get_channel_or_thread = lambda value: channel if value == 100 else None
        self.links = _Links()
        self.queries = SimpleNamespace(
            family_movement_history=AsyncMock(return_value=_history()),
        )
        self.context = AgentRequestContext(
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)),
            guild=guild, member=member,
            source_message=SimpleNamespace(
                channel=channel, created_at=datetime.now(timezone.utc),
            ),
            account_links=self.links, clan_health=self.queries, message_search=None,
        )

    async def test_report_groups_current_owner_and_filters_retained_movements(self):
        result = await read_family_account_movements(self.context, {
            "interval_limit": 1, "before_run_id": "cursor",
        })

        self.queries.family_movement_history.assert_awaited_once_with(
            interval_limit=1, before_run_id="cursor",
        )
        self.assertEqual(self.links.calls, 1)
        self.assertEqual(result["movement_count"], 3)
        self.assertEqual(result["transition_counts"], {
            "observed_entered_family": 1,
            "observed_family_clan_change": 1,
            "observed_left_family": 1,
        })
        self.assertEqual(result["current_ownership_groups_with_movements"], 2)
        linked = next(row for row in result["owner_summaries"]
                      if row["linked_member_id"] == 42)
        self.assertEqual(linked["player_tags"], ("#P0", "#P2"))
        self.assertIn("not transfer intent", result["ownership_interpretation"])
        report = self.context.state.reports[result["report_id"]]
        self.assertIsInstance(report, FamilyMovementReport)

        page = await read_family_account_movement_report(self.context, {
            "report_id": result["report_id"], "view": "movements",
            "clan_code": "bec",
        })
        self.assertEqual(page["matched_count"], 2)
        self.assertEqual(self.links.calls, 1)
        self.queries.family_movement_history.assert_awaited_once()

    async def test_no_changes_returns_coverage_without_ownership_or_artifact(self):
        self.queries.family_movement_history.return_value = _history(movements=False)
        result = await read_family_account_movements(self.context, {})
        self.assertEqual(result["movement_count"], 0)
        self.assertIsNone(result["report_id"])
        self.assertEqual(result["interval_count"], 1)
        self.assertEqual(self.links.calls, 0)
        self.assertEqual(self.context.state.reports, {})

    async def test_filters_require_movement_view_and_reports_are_scoped(self):
        result = await read_family_account_movements(self.context, {})
        rejected = await read_family_account_movement_report(self.context, {
            "report_id": result["report_id"], "member_id": 42,
        })
        self.assertIn("require", rejected["error"])
        report = self.context.state.reports[result["report_id"]]
        self.context.state.reports["role"] = RoleAccountReport("role", "now", (), ())
        self.context.state.reports["foreign"] = FamilyMovementReport(
            "foreign", 2, report.ownership_observed_at, report.history, report.rows,
        )
        for report_id in ("missing", "role", "foreign"):
            value = await read_family_account_movement_report(
                self.context, {"report_id": report_id},
            )
            self.assertIn("error", value)

    async def test_access_loss_and_report_budget_fail_without_retention(self):
        async def lose_access(**kwargs):
            self.context.member.roles = []
            return _history()

        self.queries.family_movement_history.side_effect = lose_access
        with self.assertRaises(AgentAccessLost):
            await read_family_account_movements(self.context, {})
        self.assertEqual(self.context.state.reports, {})

        self.context.member.roles = [SimpleNamespace(id=next(iter(CORE)))]
        self.queries.family_movement_history.side_effect = None
        def ownership_then_lose(tags):
            result = _Links().get_links_by_tags(tags)
            self.context.member.roles = []
            return result

        with patch.object(
            self.links, "get_links_by_tags", side_effect=ownership_then_lose,
        ), self.assertRaises(AgentAccessLost):
            await read_family_account_movements(self.context, {})
        self.assertEqual(self.context.state.reports, {})

        self.context.member.roles = [SimpleNamespace(id=next(iter(CORE)))]
        with patch("elbow_helper.features.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES", 1):
            result = await read_family_account_movements(self.context, {})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.reports, {})


if __name__ == "__main__":
    unittest.main()
