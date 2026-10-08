from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.roles import CORE
from elbow_helper.features.account_links.evidence import (
    member_account_evidence, refresh_account_locations,
)
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.research.members import find_discord_roles
from elbow_helper.infrastructure.clash.client import ClashResponse


def _account(tag, **changes):
    return dict(player_tag=tag, player_name_last_seen="Player", last_seen_clan_tag=CLANS["BEC"].tag,
                last_seen_clan_code="BEC", last_seen_role="member", **changes)


def _links(rows, response=None):
    return SimpleNamespace(
        get_links_for_members=lambda ids: {value: rows.get(value, []) for value in ids},
        get_player_location=lambda tag: None,
        clash_client=SimpleNamespace(get=AsyncMock(return_value=response)),
    )


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
        account_links=_links({1: [_account("#2PP"), _account("#2PQ")]}),
    ), role


class AgentReportTests(unittest.IsolatedAsyncioTestCase):

    async def test_feature_refresh_is_bounded_and_does_not_mutate_input(self):
        context, _ = _context(count=1)
        account = {
            "player_tag": "#2PP", "player_name": "Before", "primary": True,
            "clan_tag": None, "clan_code": None, "clan_name": None,
            "clan_role": None, "in_clan": None, "location_status": "unknown",
            "checked_at": None,
        }
        context.account_links.clash_client.get.return_value = ClashResponse(
            503, None, {}, 1, 1,
        )
        refreshed = await refresh_account_locations(context.account_links, [account])
        self.assertEqual(account["location_status"], "unknown")
        self.assertEqual(refreshed[0]["location_status"], "refresh_failed")
        with self.assertRaisesRegex(ValueError, "exceeds 25"):
            await refresh_account_locations(context.account_links, [account] * 26)

    async def test_refresh_distinguishes_no_clan_from_failed_lookup(self):
        links = _links({1: [_account("#2PP"), _account("#2PQ")]})
        links.clash_client.get.side_effect = [ClashResponse(200, {"tag": "#2PP", "name": "Unclanned"}, {}, 1, 1),
                                             ClashResponse(503, None, {}, 1, 1)]
        result = await member_account_evidence(links, [1, 2], refresh=True)
        self.assertFalse(result[1][0]["in_clan"])
        self.assertIsNone(result[1][0]["clan_tag"])
        self.assertEqual(result[1][0]["location_status"], "refreshed")
        self.assertTrue(result[1][0]["checked_at"])
        self.assertEqual(result[1][1]["clan_code"], "BEC")
        self.assertEqual(result[1][1]["location_status"], "refresh_failed")
        self.assertIsNone(result[1][1]["checked_at"])
        self.assertEqual(result[2], [])

    async def test_refresh_timeout_retains_every_account_and_cancels_requests(self):
        links = _links({1: [_account("#2PP"), _account("#2PQ")]})
        cancelled = []
        async def delayed(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        links.clash_client.get.side_effect = delayed
        with patch("elbow_helper.features.account_links.evidence.LOCATION_REFRESH_SECONDS", 0.01):
            result = await member_account_evidence(links, [1], refresh=True)
        self.assertEqual(len(result[1]), 2)
        self.assertTrue(all(row["location_status"] == "refresh_failed" for row in result[1]))
        self.assertEqual(len(cancelled), 2)

    async def test_role_discovery_uses_configured_purpose_and_full_membership(self):
        context, role = _context()
        role.name = "Brown Elbow Cat"
        result = await find_discord_roles(context, {"query": "BEC"})
        self.assertEqual(result["roles"][0]["member_count"], 12)
        self.assertIn({"clan_code": "BEC", "purpose": "member"}, result["roles"][0]["clan_purposes"])
