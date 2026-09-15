from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock

from elbow_helper.discord.message_search import DiscordSearchMessage
from elbow_helper.features.agent.tools import build_agent_tools
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
        self.assertNotIn("execute_sql", tools)

    async def test_player_health_tool_reads_through_repository_contract(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            repository = ClanHealthRepository(
                Path(temporary_directory) / "clan_health.db"
            )
            repository.initialize()
            context = SimpleNamespace(
                clan_health=SimpleNamespace(repository=repository),
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
