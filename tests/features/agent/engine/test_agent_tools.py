from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, patch

from elbow_helper.discord.message_search import DiscordSearchMessage
from elbow_helper.configuration.roles import CORE, CO_APPLICANT_ROLE_ID
from elbow_helper.features.agent.engine.registry import (
    build_agent_tool_groups, build_agent_tools,
)
from elbow_helper.features.agent.plan import capability_list, check_plan
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.help.discovery import DiscoveredCommand, ParameterInfo
from elbow_helper.features.clan_health.database import ClanHealthRepository


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



    async def test_command_help_uses_registered_public_entries_only(self):
        member = SimpleNamespace(
            id=10, roles=[
                SimpleNamespace(id=next(iter(CORE))), SimpleNamespace(id=CO_APPLICANT_ROLE_ID),
            ],
        )
        bot_member = SimpleNamespace(id=20)
        channel = _Channel(100, "chat", {10, 20})
        guild = SimpleNamespace(
            id=1, me=bot_member,
            get_member=lambda member_id: member if member_id == 10 else bot_member,
        )
        channel.guild = guild
        guild.get_channel_or_thread = lambda channel_id: channel if channel_id == 100 else None
        context = SimpleNamespace(
            bot=SimpleNamespace(), guild=guild, member=member,
            source_message=SimpleNamespace(channel=channel),
            state=AgentTurnState(source_channels={100}),
        )
        discovered = {
            "/ping": DiscoveredCommand(
                "/ping", "Check the bot's response time.",
                (ParameterInfo("target", "Example option", False, "string"),),
            ),
            "/coinlog": DiscoveredCommand("/coinlog", "Private", ()),
        }
        handler = build_agent_tools()["read_bot_command_help"].handler
        with patch(
            "elbow_helper.features.agent.commands.help_tool.discover_commands",
            return_value=discovered,
        ):
            public = await handler(context, {"query": "ping"})
            restricted = await handler(context, {"query": "coinlog"})
            listed = await handler(context, {})

        self.assertEqual(public["path"], "/ping")
        self.assertEqual(public["options"][0]["name"], "target")
        self.assertEqual(restricted["matching_count"], 0)
        self.assertNotIn("/coinlog", str(listed))

    def test_plan_catalogue_covers_every_registered_capability(self):
        registry = build_agent_tools()
        catalogue = capability_list(registry)
        self.assertEqual(len(catalogue.splitlines()), len(registry))
        for name in registry:
            self.assertEqual(catalogue.count(name + ":"), 1)
        self.assertNotIn(" | time ", catalogue)
        self.assertNotIn(" | entity ", catalogue)


    def test_oversized_plan_is_rejected_before_any_capability_runs(self):
        registry = build_agent_tools()
        plan = {
            "goal": "Read selected values", "effort": "low", "output": "text",
              "steps": [
                {"id": f"step-{index}", "capability": next(iter(registry)),
                 "arguments": {}, "reason": "Read", "depends_on": []}
                for index in range(49)
            ],
        }
        self.assertFalse(check_plan(plan, registry).ok)

    def test_duplicate_or_undescribed_capabilities_fail_closed(self):
        registry = build_agent_tools()
        duplicated = build_agent_tool_groups()
        duplicated["wars"] = (*duplicated["wars"], duplicated["files"][0])
        with patch("elbow_helper.features.agent.engine.registry.build_agent_tool_groups",
                   return_value=duplicated):
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                build_agent_tools()

        missing = dict(registry)
        missing.pop(next(iter(missing)))
        self.assertFalse(check_plan({
            "goal": "Read", "effort": "low", "output": "text",
              "steps": [
                {"id": "one", "capability": next(iter(set(registry) - set(missing))),
                 "arguments": {}, "reason": "Read", "depends_on": []},
            ],
        }, missing).ok)

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




    def test_cwl_ass_tool_preserves_selected_scope_projection(self) -> None:
        description = build_agent_tools()[
            "cwl_ass_scores"
        ].definition.description

        self.assertIn("projected to seven attacks", description)
        self.assertIn("day (round)", description)
        self.assertIn("scope and sample size", description)
        self.assertIn("do not present a partial-scope result", description)
        self.assertIn("Return every player row", description)

    def test_cwl_bonus_tool_preserves_metric_and_side_effect_boundaries(self) -> None:
        tools = build_agent_tools()
        description = tools["cwl_bonus_scores"].definition.description
        self.assertIn("adjusted-delta evidence, not ASS", description)
        self.assertIn("does not poll Clash or publish", description)
        self.assertIn("Return every player row", description)


    def test_event_tools_preserve_role_and_read_only_boundaries(self) -> None:
        tools = build_agent_tools()
        current = tools["read_event_schedule"].definition.description
        from elbow_helper.features.agent.reports.tools import original_tool
        retained = original_tool(tools, "read_saved_report", {
            "report_kind": "event_schedule",
        }).definition.description

        self.assertIn("Requires current Lead access", current)
        self.assertIn("missing counter roles explicitly", current)
        self.assertIn("does not refresh channels or change event settings", current)
        self.assertIn("Current Lead and source access are rechecked", retained)


if __name__ == "__main__":
    unittest.main()
