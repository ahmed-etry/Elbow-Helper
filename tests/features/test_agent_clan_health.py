from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.reports.clan_health import ClanHealthReport
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.reports.roles import RoleAccountReport
from elbow_helper.features.agent.tools.clan_health import (
    compare_clan_health_reports, get_clan_health, list_clan_health_reports,
    read_clan_health_period, read_clan_health_report,
)
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.clan_health.queries import ClanHealthQueries


def _row(tag, name, *, status="Good", missed=0, donations=10):
    return {
        "clan_code": "BEH", "player_tag": tag, "player_name": name,
        "status": status, "flags": ["missed"] if missed else [], "note": "",
        "war_hits_used": 2 - missed, "war_hits_expected": 2,
        "war_missed": missed, "war_attack_count": 2 - missed,
        "war_stars_total": 6 - 3 * missed, "donations": donations,
    }


class AgentClanHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = ClanHealthRepository(Path(self.temporary.name) / "health.sqlite3")
        self.repository.initialize()
        self.queries = ClanHealthQueries(self.repository)
        member = SimpleNamespace(id=42, roles=[SimpleNamespace(id=next(iter(CORE)))])
        guild = SimpleNamespace(id=1, me=member, get_member=lambda _: member)
        channel = SimpleNamespace(
            id=100, guild=guild,
            permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True),
        )
        guild.get_channel_or_thread = lambda value: channel if value == 100 else None
        self.context = AgentRequestContext(
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)),
            guild=guild, member=member,
            source_message=SimpleNamespace(channel=channel, created_at=datetime.now(timezone.utc)),
            account_links=None, clan_health=self.queries, message_search=None,
        )
        self.store("before", created=141, start=100, end=140, rows=[
            _row("#P0", "Alpha"), _row("#P2", "Beta", status="Watch"),
        ])
        self.store("after", created=241, start=200, end=240, rows=[
            _row("#P0", "Alpha", status="Needs Review", missed=1, donations=5),
            _row("#P8", "Gamma"),
        ])

    def store(self, run_id, *, created, start, end, rows):
        self.repository.store_report(
            run_id=run_id, created_ts=created, season_key=f"season-{end}",
            scope="BACKGROUND_ALL", partial=False, cycle_start_ts=start,
            cycle_end_ts=end, rows=rows,
        )

    async def test_lists_exact_completed_periods_and_reads_historical_run(self):
        listed = await list_clan_health_reports(
            self.context, {"clan_code": "beh", "limit": 1},
        )
        self.assertEqual([row["run_id"] for row in listed["reports"]], ["after"])
        previous = await list_clan_health_reports(self.context, {
            "clan_code": "BEH", "limit": 1,
            "before_run_id": listed["next_before_run_id"],
        })
        self.assertEqual([row["run_id"] for row in previous["reports"]], ["before"])
        self.assertIsNone(previous["next_before_run_id"])
        report = await read_clan_health_period(
            self.context, {"clan_code": "BEH", "run_id": "before"},
        )
        self.assertEqual(report["run"]["run_id"], "before")
        self.assertEqual(report["total_players"], 2)
        self.assertTrue(report["complete_report"])

    async def test_latest_report_retains_complete_snapshot_for_later_pages(self):
        first = await get_clan_health(self.context, {"clan_code": "BEH"})
        self.assertIsInstance(self.context.state.reports[first["report_id"]], ClanHealthReport)
        self.assertEqual(first["status_counts"], {"Good": 1, "Needs Review": 1})
        self.repository.store_report(
            run_id="replacement", created_ts=341, season_key="season-340",
            scope="BACKGROUND_ALL", partial=False, cycle_start_ts=300,
            cycle_end_ts=340, rows=[_row("#P0", "Changed")],
        )
        retained = await read_clan_health_report(
            self.context, {"report_id": first["report_id"], "offset": 1, "limit": 1},
        )
        self.assertEqual(retained["run"]["run_id"], "after")
        self.assertEqual(retained["players"][0]["player_name"], "Gamma")

    async def test_comparison_counts_all_rows_and_pages_deterministic_changes(self):
        before = await read_clan_health_period(
            self.context, {"clan_code": "BEH", "run_id": "before"},
        )
        after = await read_clan_health_period(
            self.context, {"clan_code": "BEH", "run_id": "after"},
        )
        arguments = {
            "before_report_id": before["report_id"],
            "after_report_id": after["report_id"], "limit": 1,
        }
        pages = []
        while True:
            result = await compare_clan_health_reports(self.context, arguments)
            self.assertEqual(
                result["counts"],
                {"added": 1, "removed": 1, "changed": 1, "unchanged": 0},
            )
            self.assertEqual(result["status_transitions"], {"Good -> Needs Review": 1})
            self.assertEqual(result["aggregate_deltas"]["war_missed"], 1)
            pages.extend(result["changes"])
            if result["next_offset"] is None:
                break
            arguments["offset"] = result["next_offset"]
        self.assertEqual([row["change"] for row in pages], ["changed", "added", "removed"])
        changed = pages[0]
        self.assertEqual(changed["deltas"]["war_missed"], 1)
        self.assertIn("status", changed["changed_fields"])

    async def test_comparison_rejects_other_clan_kind_guild_and_missing_report(self):
        source = await read_clan_health_period(
            self.context, {"clan_code": "BEH", "run_id": "before"},
        )
        report = self.context.state.reports[source["report_id"]]
        self.context.state.reports["role"] = RoleAccountReport("role", "now", (), ())
        self.context.state.reports["foreign"] = ClanHealthReport(
            "foreign", 2, report.snapshot,
        )
        for other in ("missing", "role", "foreign"):
            result = await compare_clan_health_reports(self.context, {
                "before_report_id": source["report_id"], "after_report_id": other,
            })
            self.assertIn("error", result)
        other = ClanHealthReport(
            "other", 1, replace(
                report.snapshot, clan_code="BEC",
                rows=tuple(replace(row, clan_code="BEC") for row in report.snapshot.rows),
            ),
        )
        self.context.state.reports[other.report_id] = other
        result = await compare_clan_health_reports(self.context, {
            "before_report_id": source["report_id"], "after_report_id": other.report_id,
        })
        self.assertIn("error", result)

    async def test_access_loss_and_artifact_budget_fail_closed(self):
        with patch("elbow_helper.features.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES", 1):
            result = await get_clan_health(self.context, {"clan_code": "BEH"})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.reports, {})
        self.context.member.roles = []
        with self.assertRaises(AgentAccessLost):
            await list_clan_health_reports(self.context, {"clan_code": "BEH"})


if __name__ == "__main__":
    unittest.main()
