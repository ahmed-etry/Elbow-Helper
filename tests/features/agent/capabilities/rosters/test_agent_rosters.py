from datetime import datetime, timezone
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from features.agent.result_path_helpers import assert_result_paths

from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.engine.service import AgentService
from tests.features.agent_plan_helpers import plan_call
from elbow_helper.features.agent.capabilities.account_links.role_report import RoleAccountReport
from elbow_helper.features.agent.capabilities.rosters.reads import compare_roster_reports, find_rosters, list_roster_cycles, read_roster, read_roster_report
from elbow_helper.features.agent.capabilities.rosters.report import RosterReport, compare_roster_reports as compare_reports
from elbow_helper.features.agent.capabilities.account_links.role_audit import read_role_account_report
from elbow_helper.features.rosters.repository import RosterRepository
from elbow_helper.features.rosters.services.queries import RosterQueries
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage


class AgentRosterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repository = RosterRepository(Path(self.directory.name) / "rosters.sqlite")
        self.roster = self.repository.create_roster(guild_id=1, name="September signup", clan_code="BEC", role_id=None, max_members=50)
        self.roster, _ = self.repository.start_cycle(self.roster.id, "2026-09")
        self.repository.add_members(self.roster.id, self.roster.active_cycle_id, 42, [
            {"player_tag": f"#TEST{index}", "player_name": f"Player {index:02}", "townhall": 16}
            for index in range(30)
        ], 50)
        member = SimpleNamespace(id=42, display_name="Tester", roles=[SimpleNamespace(id=next(iter(CORE)))])
        guild = SimpleNamespace(id=1, name="Brown Elbow", me=member, get_member=lambda _: member)
        channel = SimpleNamespace(id=100, guild=guild, permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True))
        guild.get_channel_or_thread = lambda value: channel if value == 100 else None
        self.context = AgentRequestContext(
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)), guild=guild, member=member,
            source_message=SimpleNamespace(channel=channel, created_at=datetime.now(timezone.utc)),
            account_links=None, clan_health=None, message_search=None, roster_queries=RosterQueries(self.repository),
        )

    async def test_discovery_filters_clan_and_guild(self):
        self.repository.create_roster(guild_id=2, name="Foreign", clan_code="BEC", role_id=None, max_members=50)
        result = await find_rosters(self.context, {"query": "BEC"})
        assert_result_paths(self, "find_rosters", result)
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(result["rosters"][0]["roster_id"], self.roster.id)
        self.assertEqual((await find_rosters(self.context, {"query": "no match"}))["rosters"], [])

    async def test_cycle_discovery_returns_exact_cycle_identity(self):
        result = await list_roster_cycles(self.context, {"roster_id": self.roster.id})
        assert_result_paths(self, "list_roster_cycles", result)
        self.assertEqual(result["cycles"][0]["id"], self.roster.active_cycle_id)
        self.assertIn("error", await list_roster_cycles(self.context, {"roster_id": 999}))

    async def test_full_report_pages_survive_a_database_change(self):
        first = await read_roster(self.context, {"roster_id": self.roster.id})
        assert_result_paths(self, "read_roster", first)
        self.assertEqual(first["total_accounts"], 30)
        self.assertEqual(first["total_members"], 1)
        self.assertEqual(len(first["accounts"]), 25)
        self.repository.clear_members(self.roster.id, self.roster.active_cycle_id)
        second = await read_roster_report(self.context, {"report_id": first["report_id"], "offset": first["next_offset"]})
        assert_result_paths(self, "read_roster_report", second)
        self.assertEqual(len(second["accounts"]), 5)
        self.assertEqual(second["total_accounts"], 30)
        self.assertIsNone(second["next_offset"])
        self.assertEqual(len({row["player_tag"] for row in first["accounts"] + second["accounts"]}), 30)

    async def test_invalid_cycle_and_foreign_guild_do_not_create_reports(self):
        result = await read_roster(self.context, {"roster_id": self.roster.id, "cycle_id": 999})
        self.assertIn("error", result)
        foreign = self.repository.create_roster(guild_id=2, name="Foreign", clan_code="BEC", role_id=None, max_members=50)
        self.assertIn("error", await read_roster(self.context, {"roster_id": foreign.id}))
        self.assertEqual(self.context.state.reports, {})

    async def test_only_accessible_post_locations_are_returned(self):
        self.repository.add_post(self.roster.id, 100, 101)
        self.repository.add_post(self.roster.id, 200, 201)
        result = await read_roster(self.context, {"roster_id": self.roster.id})
        assert_result_paths(self, "read_roster", result)
        self.assertEqual([item["message_id"] for item in result["accessible_posts"]], [101])
        self.assertEqual(self.context.state.source_channels, {100})

    async def test_core_loss_prevents_retained_report_reads(self):
        first = await read_roster(self.context, {"roster_id": self.roster.id})
        self.context.member.roles = []
        with self.assertRaises(AgentAccessLost):
            await read_roster_report(self.context, {"report_id": first["report_id"]})

    async def test_report_ids_are_scoped_by_conversation_and_kind(self):
        first = await read_roster(self.context, {"roster_id": self.roster.id})
        self.assertIn("error", await read_role_account_report(self.context, {"report_id": first["report_id"]}))
        self.context.state.reports["role"] = RoleAccountReport("role", "now", (), ())
        self.assertIn("error", await read_roster_report(self.context, {"report_id": "role"}))
        self.context.state.reports.clear()
        self.assertIn("error", await read_roster_report(self.context, {"report_id": first["report_id"]}))

    async def test_report_budget_rejects_whole_report_not_partial_rows(self):
        with patch("elbow_helper.features.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES", 1):
            result = await read_roster(self.context, {"roster_id": self.roster.id})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.reports, {})

    async def test_model_tool_loop_can_read_and_page_a_complete_roster(self):
        pages = []
        rounds = 0

        planned = plan_call(
            AgentToolCall("first", "read_roster", json.dumps({"roster_id": self.roster.id})),
            AgentToolCall("second", "read_saved_report", json.dumps({
                "report_kind": "roster_signups",
                "report_id": {"step": "first", "path": ["report_id"]},
                "offset": {"step": "first", "path": ["next_offset"]},
            })),
            entities=({"kind": "roster", "value": self.roster.id},
                      {"kind": "roster_report", "value": {"step": "first", "path": ["report_id"]}}),
            dependencies={"second": ("first",)},
        )

        async def advance(results=(), *, allow_tools=True, reasoning_effort=None,
                          max_output_tokens=None):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                return AgentStep("", (planned,), AgentUsage())
            result = json.loads(results[-1].content)["results"]
            pages.extend((result["first"], result["second"]))
            return AgentStep("Thirty accounts belonging to one member.", (), AgentUsage())

        model = SimpleNamespace(create_agent_session=lambda **kwargs: SimpleNamespace(
            advance=advance, replace_tools=lambda tools: None,
        ))
        result = await AgentService(model).answer(question="Read the signup roster", local_context="", context=self.context)
        self.assertIn("Thirty accounts", result)
        self.assertEqual(rounds, 2)
        self.assertEqual(sum(len(page["accounts"]) for page in pages), 30)
        self.assertEqual(pages[0]["report_id"], pages[1]["report_id"])

    async def test_comparison_counts_all_rows_and_pages_exact_changes(self):
        first = await read_roster(self.context, {"roster_id": self.roster.id})
        before = self.context.state.reports[first["report_id"]]
        members = before.snapshot.members
        after = RosterReport("after", replace(before.snapshot, members=(
            replace(members[0], discord_user_id=99, townhall=17),
            *(replace(member, signed_up_ts=member.signed_up_ts + 100) for member in members[2:]),
            replace(members[1], player_tag="#NEW"),
        )))
        self.context.state.reports[after.report_id] = after
        arguments = {"before_report_id": before.report_id, "after_report_id": after.report_id, "limit": 1}
        pages = []
        while True:
            result = await compare_roster_reports(self.context, arguments)
            self.assertEqual(result["counts"], {"added": 1, "removed": 1, "changed": 1, "unchanged": 28})
            pages.extend(result["changes"])
            if result["next_offset"] is None:
                break
            arguments["offset"] = result["next_offset"]
        self.assertEqual(len(pages), 3)
        changed = next(row for row in pages if row["change"] == "changed")
        self.assertEqual(changed["changed_fields"], ["discord_user_id", "townhall"])
        self.assertEqual(result["before"]["total_members"], 1)
        self.assertEqual(result["after"]["total_members"], 2)
        self.assertNotIn("posts", json.dumps(result))
        self.assertEqual(len(self.context.state.reports), 2)

    async def test_comparison_empty_identical_and_reversed(self):
        first = await read_roster(self.context, {"roster_id": self.roster.id})
        report = self.context.state.reports[first["report_id"]]
        empty = RosterReport("empty", replace(report.snapshot, members=()))
        self.assertEqual(compare_reports(report, report)["total_changes"], 0)
        self.assertEqual(compare_reports(empty, empty)["counts"]["unchanged"], 0)
        self.assertEqual(compare_reports(empty, report)["counts"]["added"], 30)
        self.assertEqual(compare_reports(report, empty)["counts"]["removed"], 30)
        self.assertEqual(compare_reports(report, empty, offset=30)["changes"], [])
        duplicate = RosterReport("duplicate", replace(report.snapshot, members=(report.snapshot.members[0],) * 2))
        with self.assertRaises(ValueError):
            compare_reports(report, duplicate)

    async def test_comparison_rejects_missing_wrong_kind_foreign_and_revoked_access(self):
        first = await read_roster(self.context, {"roster_id": self.roster.id})
        report = self.context.state.reports[first["report_id"]]
        self.context.state.reports["role"] = RoleAccountReport("role", "now", (), ())
        self.context.state.reports["foreign"] = RosterReport("foreign", replace(report.snapshot, roster=replace(report.snapshot.roster, guild_id=2)))
        for other in ("missing", "role", "foreign"):
            result = await compare_roster_reports(self.context, {"before_report_id": report.report_id, "after_report_id": other})
            self.assertIn("error", result)
        self.context.member.roles = []
        with self.assertRaises(AgentAccessLost):
            await compare_roster_reports(self.context, {"before_report_id": report.report_id, "after_report_id": report.report_id})

    async def test_comparison_preserves_explicit_cycle_identity(self):
        old = await read_roster(self.context, {"roster_id": self.roster.id})
        current, _ = self.repository.start_cycle(self.roster.id, "2026-10")
        new = await read_roster(self.context, {"roster_id": self.roster.id})
        result = await compare_roster_reports(self.context, {"before_report_id": old["report_id"], "after_report_id": new["report_id"]})
        self.assertEqual(result["before"]["cycle"]["cycle_key"], "2026-09")
        self.assertEqual(result["after"]["cycle"]["id"], current.active_cycle_id)
        self.assertEqual(result["counts"]["removed"], 30)
