from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.reports.historical_war import HistoricalRegularWarReport
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.reports.roles import RoleAccountReport
from elbow_helper.features.agent.tools.wars import (
    read_historical_regular_war_report,
    read_historical_regular_wars,
)
from elbow_helper.features.clan_health.queries import (
    HistoricalRegularWar,
    HistoricalRegularWarHistory,
    HistoricalRegularWarMember,
)


def _member(war_id, end_ts, tag, name, position, used, details):
    return HistoricalRegularWarMember(
        war_id, end_ts, tag, name, 18, position, 2, used, 2 - used,
        details, details == used, 3 * details, 100.0 * details, details,
    )


def _war(war_id, end_ts, *, team_size, attack_rows):
    return HistoricalRegularWar(
        war_id, "BEH", "#P0", "#P8", "Opponent", team_size, 2,
        end_ts - 2, end_ts - 1, end_ts, end_ts + 1,
        team_size, attack_rows, True, True, (),
    )


def _history():
    rows = (
        _member("new", 200, "#P0", "Alpha", 1, 1, 1),
        _member("new", 200, "#P8", "Gamma", 2, 2, 2),
        _member("old", 100, "#P2", "Beta", 1, 0, 0),
    )
    return HistoricalRegularWarHistory(
        "2026-09-17T00:00:00+00:00", "BEH",
        (_war("new", 200, team_size=2, attack_rows=3),
         _war("old", 100, team_size=1, attack_rows=0)),
        rows, "old",
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


class AgentHistoricalWarToolTests(unittest.IsolatedAsyncioTestCase):
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
        self.queries = SimpleNamespace(regular_war_history=AsyncMock(return_value=_history()))
        self.context = AgentRequestContext(
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)),
            guild=guild, member=member,
            source_message=SimpleNamespace(
                channel=channel, created_at=datetime.now(timezone.utc),
            ),
            account_links=self.links, clan_health=self.queries, message_search=None,
        )

    async def test_import_groups_current_owner_and_keeps_unlinked_gameplay(self):
        result = await read_historical_regular_wars(
            self.context, {"clan_code": "BEH", "history_limit": 2},
        )

        self.assertEqual(self.links.calls, 1)
        self.queries.regular_war_history.assert_awaited_once_with(
            "BEH", history_limit=2, before_war_id=None,
        )
        self.assertEqual(result["war_count"], 2)
        self.assertEqual(result["attacks_missed"], 3)
        self.assertEqual(result["currently_unlinked_accounts"], 1)
        self.assertEqual(result["ownership_groups_with_misses"], 1)
        self.assertEqual(len(result["member_summaries"]), 2)
        linked = next(row for row in result["member_summaries"]
                      if row["linked_member_id"] == 42)
        self.assertEqual(linked["player_tags"], ("#P0", "#P2"))
        self.assertEqual(linked["attacks_missed"], 3)
        self.assertIn("do not prove", result["ownership_interpretation"])
        report = self.context.state.reports[result["report_id"]]
        self.assertIsInstance(report, HistoricalRegularWarReport)

        page = await read_historical_regular_war_report(self.context, {
            "report_id": result["report_id"], "view": "war_rows",
            "player_tag": "p8",
        })
        self.assertEqual(page["matched_count"], 1)
        self.assertEqual(page["war_rows"][0]["source"]["player_tag"], "#P8")
        self.assertIsNone(page["war_rows"][0]["linked_member_id"])
        self.assertEqual(self.links.calls, 1)

    async def test_cursor_is_forwarded_and_filters_require_war_rows(self):
        result = await read_historical_regular_wars(self.context, {
            "clan_code": "BEH", "history_limit": 2, "before_war_id": "cursor",
        })
        self.queries.regular_war_history.assert_awaited_once_with(
            "BEH", history_limit=2, before_war_id="cursor",
        )
        filtered = await read_historical_regular_war_report(self.context, {
            "report_id": result["report_id"], "member_id": 42,
        })
        self.assertIn("require", filtered["error"])

    async def test_access_loss_and_artifact_budget_fail_atomically(self):
        async def lose_access(*args, **kwargs):
            self.context.member.roles = []
            return _history()

        self.queries.regular_war_history.side_effect = lose_access
        with self.assertRaises(AgentAccessLost):
            await read_historical_regular_wars(self.context, {"clan_code": "BEH"})
        self.assertEqual(self.context.state.reports, {})

        self.context.member.roles = [SimpleNamespace(id=next(iter(CORE)))]
        self.queries.regular_war_history.side_effect = None
        def ownership_then_lose(tags):
            result = _Links().get_links_by_tags(tags)
            self.context.member.roles = []
            return result

        with patch.object(
            self.links, "get_links_by_tags", side_effect=ownership_then_lose,
        ), self.assertRaises(AgentAccessLost):
            await read_historical_regular_wars(self.context, {"clan_code": "BEH"})
        self.assertEqual(self.context.state.reports, {})

        self.context.member.roles = [SimpleNamespace(id=next(iter(CORE)))]
        with patch("elbow_helper.features.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES", 1):
            result = await read_historical_regular_wars(
                self.context, {"clan_code": "BEH"},
            )
        self.assertIn("error", result)
        self.assertEqual(self.context.state.reports, {})

    async def test_report_read_rejects_missing_wrong_kind_and_foreign_guild(self):
        result = await read_historical_regular_wars(self.context, {"clan_code": "BEH"})
        report = self.context.state.reports[result["report_id"]]
        self.context.state.reports["role"] = RoleAccountReport("role", "now", (), ())
        self.context.state.reports["foreign"] = HistoricalRegularWarReport(
            "foreign", 2, report.ownership_observed_at, report.history, report.rows,
        )
        for report_id in ("missing", "role", "foreign"):
            with self.subTest(report_id=report_id):
                value = await read_historical_regular_war_report(
                    self.context, {"report_id": report_id},
                )
                self.assertIn("error", value)


if __name__ == "__main__":
    unittest.main()
