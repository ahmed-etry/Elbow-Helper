from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from elbow_helper.configuration.channels import SUPPORT_TICKET_CATEGORY
from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.reports.roles import RoleAccountReport
from elbow_helper.features.agent.reports.support import SupportTicketReport
from elbow_helper.features.agent.tools.support import (
    read_accessible_support_tickets,
    read_support_ticket_report,
)
from elbow_helper.features.support_tickets.queries import (
    SupportTicketQueries,
    SupportTicketSnapshot,
)


class _Channel:
    def __init__(
        self, channel_id, *, allowed=True, name="support-ticket",
        topic="<@42>", owner_can_send=True, last_message_id=None,
    ):
        self.id = channel_id
        self.allowed = allowed
        self.name = name
        self.topic = topic
        self.owner_can_send = owner_can_send
        self.last_message_id = last_message_id
        self.category_id = SUPPORT_TICKET_CATEGORY
        self.type = discord.ChannelType.text
        self.created_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.guild = None

    def permissions_for(self, actor):
        if actor.id in {10, 20}:
            allowed = self.allowed
            return SimpleNamespace(
                view_channel=allowed,
                read_message_history=allowed,
                send_messages=allowed,
            )
        return SimpleNamespace(
            view_channel=True,
            read_message_history=True,
            send_messages=self.owner_can_send,
        )


class AgentSupportToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        requester = SimpleNamespace(
            id=10, roles=[SimpleNamespace(id=next(iter(CORE)))],
        )
        bot_member = SimpleNamespace(id=20)
        owner = SimpleNamespace(id=42)
        self.source = _Channel(100, name="core", topic=None)
        self.first = _Channel(
            200, name="support-first", topic="private reason <@42>",
            owner_can_send=False,
            last_message_id=discord.utils.time_snowflake(
                datetime(2026, 9, 16, tzinfo=timezone.utc)
            ),
        )
        self.second = _Channel(300, name="support-second", topic="<@84>")
        self.hidden = _Channel(
            400, allowed=False, name="support-hidden", topic="secret <@126>",
        )
        channels = {
            row.id: row
            for row in (self.source, self.first, self.second, self.hidden)
        }
        guild = SimpleNamespace(
            id=1,
            me=bot_member,
            channels=[self.first, self.second, self.hidden],
            get_member=lambda value: (
                requester if value == 10 else owner if value == 42 else None
            ),
            get_channel_or_thread=lambda value: channels.get(value),
        )
        for channel in channels.values():
            channel.guild = guild
        real_queries = SupportTicketQueries()
        self.queries = SimpleNamespace(
            ticket_registrations=MagicMock(
                side_effect=real_queries.ticket_registrations,
            ),
            metadata_snapshot=MagicMock(
                side_effect=real_queries.metadata_snapshot,
            ),
        )
        self.context = AgentRequestContext(
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)),
            guild=guild,
            member=requester,
            source_message=SimpleNamespace(
                channel=self.source, created_at=datetime.now(timezone.utc),
            ),
            account_links=None,
            clan_health=None,
            message_search=None,
            support_queries=self.queries,
        )

    async def test_read_filters_before_interpretation_and_retains_metadata(self):
        result = await read_accessible_support_tickets(self.context, {})

        self.assertEqual(result["registered_ticket_count"], 3)
        self.assertEqual(result["accessible_ticket_count"], 2)
        self.assertEqual(result["omitted_inaccessible_count"], 1)
        selected = self.queries.metadata_snapshot.call_args.args[0]
        self.assertEqual([channel.id for channel in selected], [200, 300])
        self.assertEqual(self.context.state.source_channels, {200, 300})
        self.assertNotIn("private reason", str(result))
        self.assertNotIn("secret", str(result))
        self.assertNotIn("126", str(result))
        self.assertFalse(result["message_history_read"])
        report = self.context.state.reports[result["report_id"]]
        self.assertIsInstance(report, SupportTicketReport)

        filtered = await read_support_ticket_report(self.context, {
            "report_id": result["report_id"],
            "owner_member_id": 42,
            "owner_can_send": False,
            "activity_status": "channel_last_message",
        })
        self.assertEqual(filtered["matched_count"], 1)
        self.assertEqual(filtered["tickets"][0]["channel_id"], 200)
        self.assertEqual(self.queries.metadata_snapshot.call_count, 1)

    async def test_empty_accessible_inventory_has_coverage_without_artifact(self):
        self.first.allowed = False
        self.second.allowed = False
        result = await read_accessible_support_tickets(self.context, {})
        self.assertEqual(result["accessible_ticket_count"], 0)
        self.assertEqual(result["omitted_inaccessible_count"], 3)
        self.assertIsNone(result["report_id"])
        self.assertEqual(self.context.state.reports, {})
        self.assertEqual(self.context.state.source_channels, set())

    async def test_malformed_snapshot_does_not_retain_candidate_sources(self):
        self.queries.metadata_snapshot.side_effect = ValueError("malformed")
        result = await read_accessible_support_tickets(self.context, {})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.source_channels, set())
        self.assertEqual(self.context.state.reports, {})

    async def test_snapshot_cannot_substitute_an_unauthorized_channel(self):
        real = SupportTicketQueries().metadata_snapshot((self.hidden,))
        self.queries.metadata_snapshot.return_value = real
        self.queries.metadata_snapshot.side_effect = None
        result = await read_accessible_support_tickets(self.context, {})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.source_channels, set())

    async def test_report_reuse_rechecks_every_ticket_source(self):
        result = await read_accessible_support_tickets(self.context, {})
        self.first.allowed = False
        with self.assertRaises(AgentAccessLost):
            await read_support_ticket_report(
                self.context, {"report_id": result["report_id"]},
            )

    async def test_budget_wrong_kind_foreign_guild_and_filters_fail_closed(self):
        with patch("elbow_helper.features.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES", 1):
            result = await read_accessible_support_tickets(self.context, {})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.reports, {})

        self.context.state.source_channels.clear()
        result = await read_accessible_support_tickets(self.context, {})
        report = self.context.state.reports[result["report_id"]]
        self.context.state.reports["role"] = RoleAccountReport(
            "role", "now", (), (),
        )
        self.context.state.reports["foreign"] = SupportTicketReport(
            "foreign", 2, report.registered_ticket_count,
            report.omitted_inaccessible_count, report.snapshot,
        )
        for report_id in ("missing", "role", "foreign"):
            value = await read_support_ticket_report(
                self.context, {"report_id": report_id},
            )
            self.assertIn("error", value)
        for invalid in (
            {"channel_id": True},
            {"owner_member_id": 0},
            {"owner_can_send": "false"},
            {"activity_status": "meaningful_reply"},
        ):
            value = await read_support_ticket_report(
                self.context, {"report_id": result["report_id"], **invalid},
            )
            self.assertIn("error", value)


if __name__ == "__main__":
    unittest.main()
