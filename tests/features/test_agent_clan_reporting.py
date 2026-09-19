from datetime import datetime, timezone
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.configuration.channels import CLAN_LEADERSHIP_CHANNELS
from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.tools.clan_reporting import (
    read_missing_elder_accounts, read_missing_elder_report,
)
from elbow_helper.features.clan_reporting.queries import ClanReportingQueries


class AgentClanReportingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.first_code, self.second_code = tuple(CLAN_LEADERSHIP_CHANNELS)[:2]
        self.first_channel = CLAN_LEADERSHIP_CHANNELS[self.first_code]
        self.second_channel = CLAN_LEADERSHIP_CHANNELS[self.second_code]
        self.permissions = {50: True, self.first_channel: True, self.second_channel: False}
        self.member = SimpleNamespace(
            id=42, display_name="Requester",
            roles=[SimpleNamespace(id=next(iter(CORE)))],
        )
        self.bot_member = SimpleNamespace(id=99, roles=[])
        self.guild = SimpleNamespace(id=1, me=self.bot_member, members=[self.member])

        def channel(channel_id):
            if channel_id not in self.permissions:
                return None
            return SimpleNamespace(
                id=channel_id, guild=self.guild,
                permissions_for=lambda _: SimpleNamespace(
                    view_channel=self.permissions[channel_id],
                    read_message_history=self.permissions[channel_id],
                ),
            )

        self.guild.get_member = lambda value: (
            self.member if value == 42 else self.bot_member if value == 99 else None
        )
        self.guild.get_channel_or_thread = channel
        self.source = channel(50)
        rows = {
            self.first_code: [{
                "discord_user_id": 7,
                "discord_display_name": "Alpha",
                "player_tag": "#P2LQ",
                "player_name": "One",
                "clan_code": self.first_code,
                "ingame_role": "member",
            }],
            self.second_code: [{
                "discord_user_id": 8,
                "discord_display_name": "Hidden",
                "player_tag": "#Y8J9",
                "player_name": "Two",
                "clan_code": self.second_code,
                "ingame_role": "member",
            }],
        }
        queries = ClanReportingQueries(
            lambda code: rows[code],
            clock=lambda: datetime(2026, 9, 19, 12, tzinfo=timezone.utc),
        )
        self.context = AgentRequestContext(
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)),
            guild=self.guild, member=self.member,
            source_message=SimpleNamespace(channel=self.source),
            account_links=None, clan_health=None, message_search=None,
            clan_reporting_queries=queries,
        )

    async def test_inaccessible_clan_is_omitted_before_owner_query(self):
        first = await read_missing_elder_accounts(self.context, {
            "clan_codes": [self.first_code, self.second_code],
        })

        self.assertEqual(first["selected_clan_codes"], [self.first_code])
        self.assertEqual(
            first["omitted_inaccessible_clan_codes"], [self.second_code],
        )
        self.assertEqual(first["accounts"][0]["member_id"], 7)
        self.assertEqual(self.context.state.source_channels, {self.first_channel})

        retained = await read_missing_elder_report(self.context, {
            "report_id": first["report_id"], "member_id": 7,
        })
        self.assertEqual(retained["matching_rows"], 1)

    async def test_permission_loss_hides_retained_snapshot(self):
        first = await read_missing_elder_accounts(self.context, {
            "clan_codes": [self.first_code],
        })
        self.permissions[self.first_channel] = False

        with self.assertRaises(AgentAccessLost):
            await read_missing_elder_report(self.context, {
                "report_id": first["report_id"],
            })

    async def test_no_access_reads_no_owner_rows(self):
        calls = []
        context = replace(
            self.context,
            clan_reporting_queries=ClanReportingQueries(
                lambda code: calls.append(code) or (),
            ),
        )
        result = await read_missing_elder_accounts(context, {
            "clan_codes": [self.second_code],
        })
        self.assertIn("error", result)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
