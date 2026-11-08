from __future__ import annotations

from types import SimpleNamespace
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from elbow_helper.infrastructure.discord.message_search import DiscordSearchMessage, DiscordSearchPage
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.plan import check_plan
from elbow_helper.agent.models import AgentTurnState


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
            search_page=AsyncMock(
                return_value=DiscordSearchPage((
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
                ), 0, 5, 2, None, False, False)
            )
        )
        context = SimpleNamespace(
            private_pass=True, source_message=SimpleNamespace(channel=allowed, created_at=datetime(2026, 9, 17, tzinfo=timezone.utc)),
            guild=guild,
            member=asker,
            bot=SimpleNamespace(fetch_channel=AsyncMock()),
            message_search=message_search,
            state=AgentTurnState(),
        )
        tool = build_agent_tools()["search_discord_messages"]

        result = await tool.handler(context, {"query": "decision", "limit": 5})

        self.assertNotIn("channel_ids", message_search.search_page.await_args.kwargs)
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["content"], "visible result")
        self.assertNotIn("hidden result", str(result))


if __name__ == "__main__":
    unittest.main()
