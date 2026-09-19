from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from elbow_helper.discord.message_search import (
    DiscordHistoryPage, DiscordSearchMessage, DiscordSearchPage,
)
from elbow_helper.discord.thread_discovery import DiscordArchivedThreadPage
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.tools.discord import (
    find_discord_channels, read_discord_channel_history,
    search_discord_messages,
)
from elbow_helper.features.agent.tools.threads import find_discord_threads


def _context():
    member = SimpleNamespace(id=1)
    guild = SimpleNamespace(id=10, me=member, get_member=lambda value: member, threads=[])
    channels = [SimpleNamespace(id=value, name=f"room-{value}", guild=guild,
                permissions_for=lambda member: SimpleNamespace(view_channel=True, read_message_history=True)) for value in (100, 200)]
    guild.channels = channels
    guild.get_channel_or_thread = lambda value: next((channel for channel in channels if channel.id == value), None)
    return SimpleNamespace(
        guild=guild, member=member, state=AgentTurnState(),
        source_message=SimpleNamespace(
            id=1_000,
            created_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
        ),
        message_search=SimpleNamespace(
            search=AsyncMock(return_value=()),
            search_page=AsyncMock(return_value=DiscordSearchPage(
                (), 0, 5, 0, None, False, False,
            )),
            history_page=AsyncMock(return_value=DiscordHistoryPage(
                (), 1_000, 1, 25, None, True,
            )),
        ),
        thread_discovery=SimpleNamespace(
            active_threads=AsyncMock(return_value=()),
            archived_page=AsyncMock(return_value=DiscordArchivedThreadPage(
                (), 0, None, True, "public",
            )),
        ),
    )


def _thread(context, thread_id, *, name, private=False, archived=True):
    return SimpleNamespace(
        id=thread_id, name=name, parent_id=100, guild=context.guild,
        archived=archived,
        archive_timestamp=datetime(2026, 9, 16, tzinfo=timezone.utc),
        is_private=lambda: private,
        permissions_for=lambda _: SimpleNamespace(
            view_channel=True, read_message_history=True,
        ),
    )


class AgentSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_requested_channel_author_and_period_exclude_unrelated_matches(self):
        context = _context()
        middle = discord.utils.time_snowflake(datetime(2026, 9, 15, tzinfo=timezone.utc))
        messages = (
            DiscordSearchMessage(middle, 100, 1, "Member", "decision", ""),
        )
        context.message_search.search_page.return_value = DiscordSearchPage(
            messages, 0, 5, 1, None, False, False,
        )
        result = await search_discord_messages(context, {"query": "decision", "channel_id": 100, "author_id": 1,
                                                         "after": "2026-09-14", "before": "2026-09-16"})
        self.assertEqual(len(result["matches"]), 1)
        call = context.message_search.search_page.await_args.kwargs
        self.assertEqual(call["channel_ids"], (100,))
        self.assertEqual(call["author_id"], 1)
        self.assertLess(call["min_id"], middle)
        self.assertGreater(call["max_id"], middle)
        self.assertEqual(context.state.source_channels, {100})
        self.assertEqual(result["coverage"]["total_results_estimate"], 1)
        self.assertTrue(result["coverage"]["reached_current_indexed_end"])

    async def test_denied_channel_does_not_fall_back_to_server_search(self):
        context = _context()
        context.guild.channels[0].permissions_for = lambda member: SimpleNamespace(view_channel=False, read_message_history=False)
        result = await search_discord_messages(context, {"query": "decision", "channel_id": 100})
        self.assertIn("error", result)
        context.message_search.search.assert_not_awaited()
        context.message_search.search_page.assert_not_awaited()

    async def test_multiple_explicit_channels_are_searched_and_rechecked_together(self):
        context = _context()
        result = await search_discord_messages(context, {
            "query": "decision", "channel_ids": [100, 200], "limit": 5,
        })

        self.assertEqual(
            context.message_search.search.await_args.kwargs["channel_ids"],
            (100, 200),
        )
        self.assertEqual(context.state.source_channels, {100, 200})
        self.assertEqual(result["matches"], [])
        context.message_search.search_page.assert_not_awaited()

    async def test_multiple_channel_search_rejects_mixed_or_inaccessible_scope(self):
        context = _context()
        mixed = await search_discord_messages(context, {
            "query": "decision", "channel_id": 100,
            "channel_ids": [100, 200],
        })
        self.assertIn("error", mixed)
        context.guild.channels[1].permissions_for = lambda member: SimpleNamespace(
            view_channel=False, read_message_history=False,
        )
        denied = await search_discord_messages(context, {
            "query": "decision", "channel_ids": [100, 200],
        })
        self.assertIn("error", denied)
        context.message_search.search.assert_not_awaited()

    async def test_invalid_period_does_not_call_discord(self):
        for start, end in (("yesterday", "2026-09-15"), ("2026-09-16", "2026-09-15")):
            context = _context()
            result = await search_discord_messages(context, {"query": "decision", "after": start, "before": end})
            self.assertIn("error", result)
            context.message_search.search.assert_not_awaited()
            context.message_search.search_page.assert_not_awaited()

    async def test_short_page_keeps_server_reported_continuation(self):
        context = _context()
        message = DiscordSearchMessage(12, 100, 1, "Member", "decision", "")
        context.message_search.search_page.side_effect = (
            DiscordSearchPage((message,), 0, 5, 12, 5, False, False),
            DiscordSearchPage((message,), 5, 5, 12, 10, False, False),
        )
        first = await search_discord_messages(context, {
            "query": "decision", "channel_id": 100, "limit": 5,
        })
        context.source_message.id = 2_000
        result = await search_discord_messages(context, {
            "query": "decision", "channel_id": 100, "limit": 5,
            "cursor": first["coverage"]["next_cursor"],
        })
        self.assertEqual(result["coverage"]["returned_indexed_matches"], 1)
        self.assertEqual(result["coverage"]["next_offset"], 10)
        self.assertFalse(result["coverage"]["reached_current_indexed_end"])
        self.assertEqual(
            context.message_search.search_page.await_args.kwargs["offset"], 5,
        )
        self.assertEqual(
            context.message_search.search_page.await_args.kwargs["max_id"], 1_000,
        )
        self.assertEqual(result["coverage"]["snapshot_before_message_id"], 1_000)

    async def test_broad_search_rejects_continuation_without_calling_discord(self):
        context = _context()
        result = await search_discord_messages(
            context, {"query": "decision", "cursor": "v1.5.invalid"},
        )
        self.assertIn("error", result)
        context.message_search.search.assert_not_awaited()
        context.message_search.search_page.assert_not_awaited()

    async def test_cursor_is_bound_to_the_original_search_scope(self):
        context = _context()
        context.message_search.search_page.return_value = DiscordSearchPage(
            (), 0, 5, 10, 5, False, False,
        )
        first = await search_discord_messages(context, {
            "query": "decision", "channel_id": 100,
        })
        cursor = first["coverage"]["next_cursor"]
        context.message_search.search_page.reset_mock()

        changed = await search_discord_messages(context, {
            "query": "different", "channel_id": 100, "cursor": cursor,
        })
        self.assertIn("error", changed)
        context.message_search.search_page.assert_not_awaited()

    async def test_channel_access_is_rechecked_after_page_before_metadata_returns(self):
        context = _context()
        channel = context.guild.channels[0]

        async def search_page(**kwargs):
            channel.permissions_for = lambda member: SimpleNamespace(
                view_channel=False, read_message_history=False,
            )
            return DiscordSearchPage((), 0, 5, 3, None, False, False)

        context.message_search.search_page.side_effect = search_page
        result = await search_discord_messages(
            context, {"query": "decision", "channel_id": 100},
        )
        self.assertIn("error", result)
        self.assertNotIn("coverage", result)
        self.assertEqual(context.state.source_channels, set())

    async def test_out_of_scope_page_is_rejected_without_retaining_source(self):
        context = _context()
        context.message_search.search_page.return_value = DiscordSearchPage(
            (DiscordSearchMessage(12, 200, 1, "Member", "decision", ""),),
            0, 5, 1, None, False, False,
        )
        result = await search_discord_messages(
            context, {"query": "decision", "channel_id": 100},
        )
        self.assertIn("error", result)
        self.assertNotIn("coverage", result)
        self.assertEqual(context.state.source_channels, set())

    async def test_channel_lookup_excludes_hidden_channels(self):
        context = _context()
        context.guild.channels[1].permissions_for = lambda member: SimpleNamespace(view_channel=False, read_message_history=False)
        result = await find_discord_channels(context, {"query": "room"})
        self.assertEqual(result["channels"], [{"channel_id": 100, "name": "room-100"}])

    async def test_archived_public_thread_discovery_is_paged_and_scope_bound(self):
        context = _context()
        context.guild.channels[0].archived_threads = lambda **_: None
        threads = (
            _thread(context, 300, name="CWL decision"),
            _thread(context, 200, name="CWL notes"),
        )
        context.thread_discovery.archived_page.return_value = (
            DiscordArchivedThreadPage(
                threads, 2, 100, False, "public",
            )
        )
        first = await find_discord_threads(context, {
            "parent_channel_id": 100, "state": "archived",
            "visibility": "public", "query": "cwl", "limit": 1,
        })
        self.assertEqual(first["threads"][0]["thread_id"], 300)
        self.assertFalse(first["coverage"]["coverage_complete"])
        self.assertTrue(first["coverage"]["continuable"])
        self.assertEqual(context.state.source_channels, {100, 300})
        call = context.thread_discovery.archived_page.await_args.kwargs
        self.assertFalse(call["private"])
        self.assertFalse(call["joined"])
        self.assertEqual(call["limit"], 25)

        context.thread_discovery.archived_page.reset_mock()
        changed = await find_discord_threads(context, {
            "parent_channel_id": 100, "state": "archived",
            "visibility": "public", "query": "different", "limit": 1,
            "cursor": first["coverage"]["next_cursor"],
        })
        self.assertIn("error", changed)
        context.thread_discovery.archived_page.assert_not_awaited()

    async def test_private_archive_filters_membership_without_leaking_cursor(self):
        context = _context()
        parent = Mock(spec=discord.TextChannel)
        parent.id = 100
        parent.name = "room-100"
        parent.guild = context.guild
        parent.permissions_for = lambda _: SimpleNamespace(
            view_channel=True, read_message_history=True, manage_threads=False,
        )
        context.guild.channels[0] = parent
        hidden = _thread(
            context, 300, name="Hidden leadership", private=True,
        )
        context.thread_discovery.archived_page.return_value = (
            DiscordArchivedThreadPage(
                (hidden,), 25, hidden.id, False, "private_joined",
            )
        )

        async def access(_, channel):
            return channel is parent

        with patch(
            "elbow_helper.features.agent.tools.threads.can_access_message_channel",
            side_effect=access,
        ):
            result = await find_discord_threads(context, {
                "parent_channel_id": 100, "state": "archived",
                "visibility": "private", "limit": 10,
            })
        self.assertEqual(result["threads"], [])
        self.assertIsNone(result["coverage"]["next_cursor"])
        self.assertFalse(result["coverage"]["coverage_complete"])
        self.assertTrue(
            result["coverage"][
                "incomplete_private_scan_does_not_prove_no_matching_thread"
            ]
        )
        self.assertEqual(
            result["coverage"]["private_listing_scope"],
            "bot_visible_and_requester_joined",
        )
        self.assertNotIn("300", str(result))
        self.assertNotIn("Hidden leadership", str(result))
        self.assertEqual(context.state.source_channels, {100})

    async def test_active_private_discovery_returns_only_current_membership(self):
        context = _context()
        parent = Mock(spec=discord.TextChannel)
        parent.id = 100
        parent.name = "room-100"
        parent.guild = context.guild
        parent.permissions_for = lambda _: SimpleNamespace(
            view_channel=True, read_message_history=True, manage_threads=False,
        )
        context.guild.channels[0] = parent
        allowed = _thread(
            context, 300, name="Allowed", private=True, archived=False,
        )
        hidden = _thread(
            context, 200, name="Hidden", private=True, archived=False,
        )
        context.thread_discovery.active_threads.return_value = (allowed, hidden)

        async def access(_, channel):
            return channel is parent or channel is allowed

        with patch(
            "elbow_helper.features.agent.tools.threads.can_access_message_channel",
            side_effect=access,
        ):
            result = await find_discord_threads(context, {
                "parent_channel_id": 100, "state": "active",
                "visibility": "private", "limit": 10,
            })
        self.assertEqual(
            [thread["thread_id"] for thread in result["threads"]], [300],
        )
        self.assertFalse(result["coverage"]["coverage_complete"])
        self.assertEqual(context.state.source_channels, {100, 300})

    async def test_parent_access_loss_after_archive_read_discards_metadata(self):
        context = _context()
        parent = context.guild.channels[0]
        parent.archived_threads = lambda **_: None

        async def discover(*_, **__):
            parent.permissions_for = lambda _: SimpleNamespace(
                view_channel=False, read_message_history=False,
            )
            return DiscordArchivedThreadPage((), 0, None, True, "public")

        context.thread_discovery.archived_page.side_effect = discover
        result = await find_discord_threads(context, {
            "parent_channel_id": 100, "state": "archived",
            "visibility": "public",
        })
        self.assertIn("error", result)
        self.assertNotIn("coverage", result)
        self.assertEqual(context.state.source_channels, set())

    async def test_history_cursor_freezes_scope_and_filters_author(self):
        context = _context()
        upper = discord.utils.time_snowflake(
            datetime(2026, 9, 17, tzinfo=timezone.utc),
        )
        context.source_message.id = upper
        after = discord.utils.time_snowflake(
            datetime(2026, 9, 1, tzinfo=timezone.utc),
        ) - 1
        first_id = upper - 100
        first_message = DiscordSearchMessage(
            first_id, 100, 2, "Other", "first", "2026-09-16T00:00:00+00:00",
        )
        context.message_search.history_page.side_effect = (
            DiscordHistoryPage((first_message,), upper, after, 1, first_id, False),
            DiscordHistoryPage((), first_id, after, 1, None, True),
        )
        first = await read_discord_channel_history(context, {
            "channel_id": 100, "after": "2026-09-01", "author_id": 1,
            "limit": 1,
        })
        self.assertEqual(first["messages"], [])
        self.assertIsNotNone(first["coverage"]["next_cursor"])
        self.assertEqual(first["coverage"]["scanned_messages_in_window"], 1)
        context.source_message.id = 2_000
        second = await read_discord_channel_history(context, {
            "channel_id": 100, "after": "2026-09-01", "author_id": 1,
            "limit": 1, "cursor": first["coverage"]["next_cursor"],
        })
        self.assertTrue(second["coverage"]["reached_requested_start"])
        call = context.message_search.history_page.await_args.kwargs
        self.assertEqual(call["before_id"], first_id)
        self.assertEqual(second["coverage"]["snapshot_before_message_id"], upper)

    async def test_history_cursor_rejects_changed_scope_before_read(self):
        context = _context()
        upper = discord.utils.time_snowflake(
            datetime(2026, 9, 17, tzinfo=timezone.utc),
        )
        context.source_message.id = upper
        first_id = upper - 100
        message = DiscordSearchMessage(
            first_id, 100, 1, "Member", "first", "2026-09-16T00:00:00+00:00",
        )
        after = discord.utils.time_snowflake(
            datetime(2026, 9, 1, tzinfo=timezone.utc),
        ) - 1
        context.message_search.history_page.return_value = DiscordHistoryPage(
            (message,), upper, after, 1, first_id, False,
        )
        first = await read_discord_channel_history(context, {
            "channel_id": 100, "after": "2026-09-01", "limit": 1,
        })
        context.message_search.history_page.reset_mock()
        changed = await read_discord_channel_history(context, {
            "channel_id": 100, "after": "2026-09-02", "limit": 1,
            "cursor": first["coverage"]["next_cursor"],
        })
        self.assertIn("error", changed)
        context.message_search.history_page.assert_not_awaited()

    async def test_history_rechecks_access_after_discord_before_returning_data(self):
        context = _context()
        context.source_message.id = discord.utils.time_snowflake(
            datetime(2026, 9, 17, tzinfo=timezone.utc),
        )
        channel = context.guild.channels[0]
        after = discord.utils.time_snowflake(
            datetime(2026, 9, 1, tzinfo=timezone.utc),
        ) - 1

        async def history_page(**kwargs):
            channel.permissions_for = lambda member: SimpleNamespace(
                view_channel=False, read_message_history=False,
            )
            return DiscordHistoryPage((), kwargs["before_id"], after, 25, None, True)

        context.message_search.history_page.side_effect = history_page
        result = await read_discord_channel_history(context, {
            "channel_id": 100, "after": "2026-09-01",
        })
        self.assertIn("error", result)
        self.assertNotIn("coverage", result)
        self.assertEqual(context.state.source_channels, set())

    async def test_history_rejects_out_of_scope_messages(self):
        context = _context()
        upper = discord.utils.time_snowflake(
            datetime(2026, 9, 17, tzinfo=timezone.utc),
        )
        context.source_message.id = upper
        after = discord.utils.time_snowflake(
            datetime(2026, 9, 1, tzinfo=timezone.utc),
        ) - 1
        context.message_search.history_page.return_value = DiscordHistoryPage(
            (DiscordSearchMessage(
                upper - 100, 200, 1, "Member", "wrong", "2026-09-16T00:00:00+00:00",
            ),),
            upper, after, 25, None, True,
        )
        result = await read_discord_channel_history(context, {
            "channel_id": 100, "after": "2026-09-01",
        })
        self.assertIn("error", result)
        self.assertEqual(context.state.source_channels, set())
