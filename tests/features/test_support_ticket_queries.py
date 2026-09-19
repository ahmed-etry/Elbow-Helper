from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import discord

from elbow_helper.configuration.channels import SUPPORT_TICKET_CATEGORY
from elbow_helper.features.support_tickets.queries import (
    SupportTicketQueries,
    parse_support_owner_id,
)


class _Channel:
    def __init__(
        self, channel_id, *, category_id=SUPPORT_TICKET_CATEGORY,
        channel_type=discord.ChannelType.text, name="ticket",
        topic=None, created_at=None, last_message_id=None, owner_can_send=True,
    ):
        self.id = channel_id
        self.category_id = category_id
        self.type = channel_type
        self.name = name
        self.topic = topic
        self.created_at = created_at or datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.last_message_id = last_message_id
        self.owner_can_send = owner_can_send
        self.guild = None

    def permissions_for(self, member):
        del member
        return SimpleNamespace(send_messages=self.owner_can_send)


class SupportTicketQueryTests(unittest.TestCase):
    def setUp(self):
        self.observed = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)

    def test_registration_selects_only_text_channels_in_support_category(self):
        selected = _Channel(20)
        earlier = _Channel(10)
        wrong_category = _Channel(30, category_id=999)
        wrong_type = _Channel(40, channel_type=discord.ChannelType.voice)
        guild = SimpleNamespace(
            channels=[selected, wrong_type, earlier, wrong_category],
        )

        registrations = SupportTicketQueries().ticket_registrations(guild)

        self.assertEqual([row.channel_id for row in registrations], [10, 20])

    def test_snapshot_exposes_metadata_without_message_history_or_topic_text(self):
        last_activity = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
        resolved = _Channel(
            10, name="support-member", topic="private reason <@42>",
            last_message_id=discord.utils.time_snowflake(last_activity),
            owner_can_send=False,
        )
        unknown = _Channel(20, name="support-unknown", topic="<@84>")
        unidentified = _Channel(30, name="support-no-owner", topic="private note")
        owner = SimpleNamespace(id=42)
        guild = SimpleNamespace(get_member=lambda value: owner if value == 42 else None)
        for channel in (resolved, unknown, unidentified):
            channel.guild = guild

        snapshot = SupportTicketQueries().metadata_snapshot(
            (resolved, unknown, unidentified), observed_at=self.observed,
        )

        rows = {row.channel_id: row for row in snapshot.tickets}
        self.assertEqual(rows[10].owner_status, "resolved")
        self.assertFalse(rows[10].owner_can_send)
        self.assertEqual(rows[10].last_activity_age_seconds, 86_400)
        self.assertEqual(rows[20].owner_status, "not_in_guild_cache")
        self.assertIsNone(rows[20].owner_can_send)
        self.assertEqual(rows[30].owner_status, "unidentified")
        self.assertNotIn("private reason", repr(snapshot))
        self.assertNotIn("private note", repr(snapshot))
        self.assertFalse(hasattr(resolved, "history"))

    def test_snapshot_rejects_duplicate_invalid_and_over_bound_selections(self):
        channel = _Channel(10)
        channel.guild = SimpleNamespace(get_member=lambda _: None)
        queries = SupportTicketQueries()
        with self.assertRaises(ValueError):
            queries.metadata_snapshot((channel, channel), observed_at=self.observed)
        with self.assertRaises(ValueError):
            queries.metadata_snapshot(
                (_Channel(20, category_id=999),), observed_at=self.observed,
            )
        with patch(
            "elbow_helper.features.support_tickets.queries.MAX_SUPPORT_TICKET_CHANNELS",
            1,
        ), self.assertRaises(ValueError):
            queries.metadata_snapshot(
                (_Channel(20), _Channel(30)), observed_at=self.observed,
            )

    def test_owner_parser_preserves_supported_topic_forms(self):
        self.assertEqual(parse_support_owner_id("<@42>"), 42)
        self.assertEqual(parse_support_owner_id("owner <@!84> detail"), 84)
        self.assertEqual(parse_support_owner_id("126"), 126)
        self.assertEqual(parse_support_owner_id("0"), 0)
        self.assertIsNone(parse_support_owner_id("owner unknown"))
        self.assertIsNone(parse_support_owner_id(None))


if __name__ == "__main__":
    unittest.main()
