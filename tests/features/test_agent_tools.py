from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, patch

from elbow_helper.discord.message_search import DiscordSearchMessage
from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.tools import (
    build_agent_tool_groups, build_agent_tools,
)
from elbow_helper.features.agent.tool_selection import ToolSelection
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.clan_health.queries import ClanHealthQueries


class _Channel:
    def __init__(self, channel_id: int, name: str, visible_members: set[int]):
        self.id = channel_id
        self.name = name
        self.guild = None
        self._visible_members = visible_members

    def permissions_for(self, member):
        visible = member.id in self._visible_members
        return SimpleNamespace(
            view_channel=visible,
            read_message_history=visible,
        )


class AgentToolTests(unittest.IsolatedAsyncioTestCase):
    def test_capability_selection_replaces_optional_groups(self):
        registry = build_agent_tools()
        selection = ToolSelection.for_registry(
            registry, build_agent_tool_groups(),
        )
        self.assertIsNotNone(selection)

        first = selection.activate(["files", "rosters"])
        self.assertNotIn("error", first)
        self.assertIn("list_supported_attachments", selection.active_names)
        self.assertIn("read_roster", selection.active_names)
        second = selection.activate(["wars"])

        self.assertNotIn("error", second)
        self.assertIn("read_regular_war", selection.active_names)
        self.assertIn("read_conversation_history", selection.active_names)
        self.assertNotIn("list_supported_attachments", selection.active_names)
        self.assertNotIn("read_roster", selection.active_names)

    def test_oversized_selection_preserves_previous_active_groups(self):
        selection = ToolSelection.for_registry(
            build_agent_tools(), build_agent_tool_groups(),
        )
        self.assertIsNotNone(selection)
        selection.activate(["wars"])
        previous = set(selection.active_names)

        with patch(
            "elbow_helper.features.agent.tool_selection."
            "MAX_ACTIVE_CAPABILITY_TOOLS",
            1,
        ):
            result = selection.activate([
                "discord_research", "members_roles", "cwl", "member_cases",
            ])

        self.assertIn("error", result)
        self.assertEqual(selection.active_names, previous)

    def test_duplicate_or_undescribed_capability_groups_fail_closed(self):
        registry = build_agent_tools()
        duplicated = build_agent_tool_groups()
        duplicated["wars"] = (*duplicated["wars"], duplicated["files"][0])
        with self.assertRaisesRegex(ValueError, "exactly one"):
            ToolSelection.for_registry(registry, duplicated)

        missing = build_agent_tool_groups()
        missing.pop("files")
        with self.assertRaisesRegex(ValueError, "summaries differ"):
            ToolSelection.for_registry(registry, missing)

    def test_completed_player_report_ignores_newer_unfinished_period(self):
        with TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / "health.db")
            repository.initialize()
            with closing(sqlite3.connect(repository.path)) as connection, connection:
                for run_id, period_end, partial in (("finished", 100, 0), ("future", 300, 0), ("partial", 150, 1)):
                    connection.execute("INSERT INTO report_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                                       (run_id, 180, "2026-09", "BACKGROUND_ALL", partial, 0, period_end))
                    connection.execute("INSERT INTO report_players (run_id, season_key, clan_code, player_tag, player_name, status, flags_json, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                       (run_id, "2026-09", "BEH", "#2PP", "Player", "Good", "[]", ""))
            result = repository.completed_player_report("#2PP", 200)
            self.assertEqual(result["run_id"], "finished")
            self.assertEqual(result["cycle_end_ts"], 100)

    async def test_message_search_is_limited_to_asker_access(self) -> None:
        asker = SimpleNamespace(id=10)
        bot_member = SimpleNamespace(id=20)
        allowed = _Channel(100, "core-chat", {10, 20})
        hidden = _Channel(200, "other-private", {20})
        guild = SimpleNamespace(
            id=1,
            me=bot_member,
            channels=[allowed, hidden],
            threads=[],
            get_member=lambda member_id: asker if member_id == 10 else bot_member,
        )
        allowed.guild = guild
        hidden.guild = guild
        guild.get_channel_or_thread = lambda channel_id: {
            100: allowed,
            200: hidden,
        }.get(channel_id)
        message_search = SimpleNamespace(
            search=AsyncMock(
                return_value=(
                    DiscordSearchMessage(
                        message_id=1,
                        channel_id=100,
                        author_id=30,
                        author_name="Allowed",
                        content="visible result",
                        timestamp="2026-09-14T00:00:00+00:00",
                    ),
                    DiscordSearchMessage(
                        message_id=2,
                        channel_id=200,
                        author_id=40,
                        author_name="Hidden",
                        content="hidden result",
                        timestamp="2026-09-14T00:00:00+00:00",
                    ),
                )
            )
        )
        context = SimpleNamespace(
            guild=guild,
            member=asker,
            bot=SimpleNamespace(fetch_channel=AsyncMock()),
            message_search=message_search,
            state=AgentTurnState(),
        )
        tool = build_agent_tools()["search_discord_messages"]

        result = await tool.handler(context, {"query": "decision", "limit": 5})

        self.assertEqual(
            message_search.search.await_args.kwargs["channel_ids"],
            (100,),
        )
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["content"], "visible result")
        self.assertNotIn("hidden result", str(result))



    def test_beta_tool_catalogue_includes_clan_health(self) -> None:
        tools = build_agent_tools()

        self.assertIn("find_clan_health_players", tools)
        self.assertIn("get_player_health", tools)
        self.assertIn("get_clan_health", tools)
        self.assertIn("read_member_achievements", tools)
        self.assertIn("read_member_achievement_report", tools)
        self.assertIn("read_achievement_economy_rules", tools)
        self.assertIn("read_achievement_leaderboard", tools)
        self.assertIn("read_achievement_leaderboard_report", tools)
        self.assertIn("read_event_schedule", tools)
        self.assertIn("read_event_schedule_report", tools)
        self.assertIn("list_clan_health_reports", tools)
        self.assertIn("read_clan_health_period", tools)
        self.assertIn("read_clan_health_report", tools)
        self.assertIn("compare_clan_health_reports", tools)
        self.assertIn("read_family_account_movements", tools)
        self.assertIn("read_family_account_movement_report", tools)
        self.assertIn("read_pending_transfer_requests", tools)
        self.assertIn("read_pending_transfer_report", tools)
        self.assertIn("read_active_hibernation", tools)
        self.assertIn("read_active_hibernation_report", tools)
        self.assertIn("read_accessible_support_tickets", tools)
        self.assertIn("read_support_ticket_report", tools)
        self.assertIn("read_active_recruitment_trials", tools)
        self.assertIn("read_active_recruitment_trial_report", tools)
        self.assertIn("read_accessible_examination_cases", tools)
        self.assertIn("read_examination_case_report", tools)
        self.assertIn("read_active_leadership_records", tools)
        self.assertIn("read_leadership_record_report", tools)
        self.assertIn("read_discord_channel_history", tools)
        self.assertIn("find_discord_threads", tools)
        self.assertIn("compare_role_account_reports", tools)
        self.assertIn("start_discord_research_job", tools)
        self.assertIn("start_discord_history_job", tools)
        self.assertIn("continue_discord_research_job", tools)
        self.assertIn("read_discord_research_job", tools)
        self.assertIn("cancel_discord_research_job", tools)
        self.assertIn("retain_discord_research_report", tools)
        self.assertIn("read_discord_research_report", tools)
        self.assertIn("list_regular_war_status", tools)
        self.assertIn("read_regular_war", tools)
        self.assertIn("read_regular_war_report", tools)
        self.assertIn("read_historical_regular_wars", tools)
        self.assertIn("read_historical_regular_war_report", tools)
        self.assertIn("read_cwl_performance", tools)
        self.assertIn("read_cwl_performance_report", tools)
        self.assertIn("list_cwl_ass_seasons", tools)
        self.assertIn("read_cwl_ass_scope", tools)
        self.assertIn("read_cwl_ass_scope_report", tools)
        self.assertIn("read_cwl_bonus_scope", tools)
        self.assertIn("read_cwl_bonus_scope_report", tools)
        self.assertIn("read_cwl_threads", tools)
        self.assertIn("list_supported_attachments", tools)
        self.assertIn("import_csv_attachment", tools)
        self.assertIn("read_csv_import", tools)
        self.assertIn("import_xlsx_attachment", tools)
        self.assertIn("read_xlsx_import", tools)
        self.assertIn("import_text_attachment", tools)
        self.assertIn("read_text_import", tools)
        self.assertNotIn("execute_sql", tools)
        self.assertNotIn("propose_action", tools)
        self.assertNotIn("approve_action", tools)
        self.assertNotIn("execute_action", tools)

    def test_cwl_ass_tool_preserves_selected_scope_projection(self) -> None:
        description = build_agent_tools()[
            "read_cwl_ass_scope"
        ].definition.description

        self.assertIn("projected to seven attacks", description)
        self.assertIn("day (round)", description)
        self.assertIn("scope and sample size", description)
        self.assertIn("do not present a partial-scope result", description)
        self.assertIn("requested spreadsheet", description)

    def test_cwl_bonus_tool_preserves_metric_and_side_effect_boundaries(self) -> None:
        tools = build_agent_tools()
        description = tools["read_cwl_bonus_scope"].definition.description
        retained = tools[
            "read_cwl_bonus_scope_report"
        ].definition.description

        self.assertIn("adjusted-delta evidence, not ASS", description)
        self.assertIn("does not poll Clash or publish", description)
        self.assertIn("requested spreadsheet", description)
        self.assertIn("without recalculating", retained)
        self.assertIn("distinct from ASS", retained)

    def test_achievement_tools_are_read_only_and_distinguish_progress(self) -> None:
        tools = build_agent_tools()
        progress = tools["read_member_achievements"].definition.description
        leaderboard = tools[
            "read_achievement_leaderboard"
        ].definition.description

        self.assertIn("existing achievement rules", progress)
        self.assertIn("Completion-only", progress)
        self.assertIn("does not expose coin transactions", progress)
        self.assertIn(
            "not an activity, value or leadership ranking", leaderboard,
        )
        self.assertIn("current non-Lead server members", leaderboard)
        self.assertIn("no economy or raffle action", leaderboard)

    def test_event_tools_preserve_role_and_read_only_boundaries(self) -> None:
        tools = build_agent_tools()
        current = tools["read_event_schedule"].definition.description
        retained = tools["read_event_schedule_report"].definition.description

        self.assertIn("Requires current Lead access", current)
        self.assertIn("missing counter roles explicitly", current)
        self.assertIn("does not refresh channels or change event settings", current)
        self.assertIn("Current Lead and source access are rechecked", retained)

    async def test_player_health_tool_reads_through_repository_contract(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            repository = ClanHealthRepository(
                Path(temporary_directory) / "clan_health.db"
            )
            repository.initialize()
            member = SimpleNamespace(id=10, roles=[SimpleNamespace(id=next(iter(CORE)))])
            channel = _Channel(100, "core-chat", {10, 20})
            guild = SimpleNamespace(
                id=1, me=SimpleNamespace(id=20),
                get_member=lambda member_id: member if member_id == 10 else None,
            )
            channel.guild = guild
            guild.get_channel_or_thread = lambda channel_id: channel if channel_id == 100 else None
            context = SimpleNamespace(
                clan_health=ClanHealthQueries(repository),
                guild=guild, member=member, state=AgentTurnState(),
                source_message=SimpleNamespace(channel=channel),
            )
            tool = build_agent_tools()["get_player_health"]

            result = await tool.handler(
                context,
                {"player_tag": "#2PP", "days": 30},
            )

        self.assertEqual(result["player_tag"], "#2PP")
        self.assertEqual(result["window"]["days"], 30)
        self.assertEqual(result["window_activity"]["war"], {})
        self.assertEqual(result["recent_war_attacks"], [])


if __name__ == "__main__":
    unittest.main()
