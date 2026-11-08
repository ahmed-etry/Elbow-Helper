from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.roles import CORE
from elbow_helper.agent.models import AgentTurnState
from elbow_helper.agent.research.members import find_discord_roles


def _context(count=12):
    role = SimpleNamespace(id=CLANS["BEC"].member_role_id, name="BEC member", position=3, managed=False, permissions=[])
    members = [SimpleNamespace(id=value, display_name=f"Member {value:02}", roles=[role]) for value in range(1, count + 1)]
    core_role = SimpleNamespace(id=next(iter(CORE)))
    requester = SimpleNamespace(id=999, display_name="Requester", roles=[core_role])
    bot_member = SimpleNamespace(id=998, display_name="Bot", roles=[])
    guild = SimpleNamespace(
        id=1, chunked=True, members=members, roles=[role],
        get_role=lambda value: role if value == role.id else None,
        get_member=lambda value: requester if value == requester.id else None,
        me=bot_member, filesize_limit=10_000_000,
    )
    channel = SimpleNamespace(
        id=100, guild=guild,
        permissions_for=lambda _: SimpleNamespace(
            view_channel=True, read_message_history=True,
        ),
    )
    guild.get_channel_or_thread = lambda value: channel if value == channel.id else None
    return SimpleNamespace(
        guild=guild, member=requester, state=AgentTurnState(),
        source_message=SimpleNamespace(channel=channel),
        bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)),
    ), role


class AgentReportTests(unittest.IsolatedAsyncioTestCase):


    async def test_role_discovery_uses_configured_purpose_and_full_membership(self):
        context, role = _context()
        role.name = "Brown Elbow Cat"
        result = await find_discord_roles(context, {"query": "BEC"})
        self.assertEqual(result["roles"][0]["member_count"], 12)
        self.assertIn({"clan_code": "BEC", "purpose": "member"}, result["roles"][0]["clan_purposes"])
