from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from elbow_helper.infrastructure.discord.message_search import (
    DiscordHistoryPage, DiscordSearchMessage, DiscordSearchPage,
)
from elbow_helper.infrastructure.discord.thread_discovery import DiscordArchivedThreadPage
from elbow_helper.agent.models import AgentTurnState
from elbow_helper.agent.research.history import find_discord_channels
from elbow_helper.agent.research.history import read_discord_channel_history
from elbow_helper.agent.research.search import search_discord_messages
from elbow_helper.agent.research.threads import find_discord_threads


def _context():
    member = SimpleNamespace(id=1)
    guild = SimpleNamespace(id=10, me=member, get_member=lambda value: member, threads=[])
    channels = [SimpleNamespace(id=value, name=f"room-{value}", guild=guild,
                permissions_for=lambda member: SimpleNamespace(view_channel=True, read_message_history=True)) for value in (100, 200)]
    guild.channels = channels
    guild.get_channel_or_thread = lambda value: next((channel for channel in channels if channel.id == value), None)
    return SimpleNamespace(private_pass=True,
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


class MentionSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_mentions_reach_transport_and_bind_continuation_scope(self):
        context = _context()
        context.message_search.search_page.return_value = DiscordSearchPage((), 0, 5, 10, 5, False, False)
        first = await search_discord_messages(context, {"channel_id": 100, "mentions": [42]})
        self.assertEqual(context.message_search.search_page.await_args.kwargs["mentions"], [42])
        mismatch = await search_discord_messages(context, {
            "channel_id": 100, "mentions": [43], "cursor": first["coverage"]["next_cursor"],
        })
        self.assertIn("error", mismatch)
        context.message_search.search_page.assert_awaited_once()

    async def test_mentions_only_search_is_valid(self):
        context = _context()
        result = await search_discord_messages(context, {"mentions": [42]})
        self.assertNotIn("error", result)
        self.assertEqual(context.message_search.search_page.await_args.kwargs["mentions"], [42])


class ServerSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_words_only_server_search_pages_once_without_channels_or_totals(self):
        context = _context()
        context.message_search.search_page.side_effect = (
            DiscordSearchPage((DiscordSearchMessage(20, 100, 1, "Synthetic", "first", ""),),
                              0, 25, 80, 25, False, False),
            DiscordSearchPage((DiscordSearchMessage(10, 200, 2, "Synthetic", "second", ""),),
                              25, 25, 80, 50, False, False),
        )
        first = await search_discord_messages(context, {"query": "topic"})
        second = await search_discord_messages(context, {
            "query": "topic", "cursor": first["coverage"]["next_cursor"],
        })
        self.assertEqual([row["message_id"] for row in first["matches"]], [20])
        self.assertEqual([row["message_id"] for row in second["matches"]], [10])
        self.assertEqual(context.message_search.search_page.await_count, 2)
        for call, offset in zip(context.message_search.search_page.await_args_list, (0, 25)):
            self.assertNotIn("channel_ids", call.kwargs)
            self.assertEqual(call.kwargs["offset"], offset)
            self.assertEqual(call.kwargs["limit"], 25)
            self.assertEqual(call.kwargs["content"], "topic")
            self.assertEqual(call.kwargs["max_id"], 1000)
        for page in (first, second):
            self.assertNotIn("total_results_estimate", page["coverage"])
        context.message_search.search.assert_not_awaited()

    async def test_hidden_and_nonsearchable_results_dropped_but_empty_page_continues(self):
        context = _context()
        context.guild.channels[1].permissions_for = lambda _: SimpleNamespace(
            view_channel=False, read_message_history=False,
        )
        context.message_search.search_page.return_value = DiscordSearchPage((
            DiscordSearchMessage(20, 200, 2, "Hidden", "secret", ""),
            DiscordSearchMessage(10, 300, 3, "Unknown", "secret", ""),
        ), 0, 25, 80, 25, False, False)
        result = await search_discord_messages(context, {"query": "topic"})
        self.assertEqual(result["matches"], [])
        self.assertIsNotNone(result["coverage"]["next_cursor"])
        self.assertNotIn("total_results_estimate", result["coverage"])
        self.assertNotIn("returned_indexed_matches", result["coverage"])
        self.assertNotIn("secret", str(result))
        self.assertEqual(context.state.source_channels, set())

    async def test_explicit_accessible_channels_report_indexed_matches_and_server_wide_does_not(self):
        context = _context()
        context.message_search.search_page.return_value = DiscordSearchPage(
            (DiscordSearchMessage(10, 100, 1, "Member", "topic", ""),), 0, 25, 1, None, False, False)
        explicit = await search_discord_messages(context, {"query": "topic", "channel_id": 100})
        self.assertEqual(explicit["coverage"]["returned_indexed_matches"], 1)
        broad = await search_discord_messages(context, {"query": "topic"})
        self.assertNotIn("returned_indexed_matches", broad["coverage"])

    async def test_invalid_author_ids_ask_for_author_ids(self):
        context = _context()
        for arguments in ({"author_ids": [0]}, {"author_id": -1}, {"author_ids": ["x"]}):
            with self.subTest(arguments=arguments):
                result = await search_discord_messages(context, {"query": "topic", **arguments})
                self.assertEqual(result, {"error": "Supply valid author member IDs."})
        context.message_search.search_page.assert_not_awaited()

    async def test_server_rechecks_readability_after_search(self):
        context = _context()
        async def page(**kwargs):
            context.guild.channels[0].permissions_for = lambda _: SimpleNamespace(
                view_channel=False, read_message_history=False,
            )
            return DiscordSearchPage((DiscordSearchMessage(20, 100, 1, "Hidden", "secret", ""),),
                                     0, 25, 80, 25, False, False)
        context.message_search.search_page.side_effect = page
        result = await search_discord_messages(context, {"query": "topic"})
        self.assertEqual(result["matches"], [])
        self.assertNotIn("total_results_estimate", result["coverage"])
        self.assertIsNotNone(result["coverage"]["next_cursor"])

    async def test_server_cursor_bound_to_asker_and_every_filter(self):
        arguments = {"query": "topic", "author_ids": [1, 2], "mentions": [3, 4],
                     "after": "2015-01-01", "before": "2026-09-01"}
        for change in ({"query": "other"}, {"author_ids": [1]}, {"mentions": [3]},
                       {"after": "2016-01-01"}, {"before": "2026-08-01"},
                       {"channel_ids": [100]}, {"requester": 2}):
            with self.subTest(change=change):
                context = _context()
                context.message_search.search_page.return_value = DiscordSearchPage(
                    (), 0, 25, 80, 25, False, False)
                first = await search_discord_messages(context, arguments)
                changed = dict(change)
                if "requester" in changed:
                    context.member = SimpleNamespace(id=changed.pop("requester"))
                result = await search_discord_messages(context, {
                    **arguments, **changed, "cursor": first["coverage"]["next_cursor"],
                })
                self.assertIn("error", result)
                context.message_search.search_page.assert_awaited_once()

    async def test_two_authors_in_one_search(self):
        context = _context()
        context.message_search.search_page.return_value = DiscordSearchPage(tuple(
            DiscordSearchMessage(identifier, 100, identifier, "Synthetic", "body", "")
            for identifier in (1, 2, 3)), 0, 25, 3, None, False, False)
        result = await search_discord_messages(context, {"author_ids": [1, 2]})
        self.assertEqual([row["author_id"] for row in result["matches"]], [1, 2])
        self.assertEqual(context.message_search.search_page.await_args.kwargs["author_ids"], (1, 2))
        context.message_search.search_page.assert_awaited_once()

    async def test_invalid_typed_filters_do_not_fetch_discord(self):
        for arguments in ({"author_ids": "1"}, {"author_ids": [True]}, {"author_id": [1, 2]},
                          {"channel_ids": "100"}, {"channel_ids": [[]]}, {"mentions": [0]}):
            with self.subTest(arguments=arguments):
                context = _context()
                result = await search_discord_messages(context, arguments)
                self.assertIn("error", result)
                context.message_search.search_page.assert_not_awaited()

    async def test_multichannel_cursor_continues_after_unavailable_channel_is_dropped(self):
        context = _context()
        context.message_search.search_page.return_value = DiscordSearchPage((), 0, 25, 80, 25, False, False)
        arguments = {"query": "topic", "channel_ids": [100, 200]}
        first = await search_discord_messages(context, arguments)
        context.guild.channels[1].permissions_for = lambda _: SimpleNamespace(
            view_channel=False, read_message_history=False,
        )
        second = await search_discord_messages(context, {
            **arguments, "cursor": first["coverage"]["next_cursor"],
        })
        self.assertNotIn("error", second)
        self.assertEqual(second["missing_sources"], [{"channel_id": 200, "cause": "source_unavailable"}])
        self.assertNotIn("total_results_estimate", second["coverage"])
        call = context.message_search.search_page.await_args.kwargs
        self.assertEqual(call["channel_ids"], (100,))
        self.assertEqual(call["offset"], 25)


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
    async def test_search_results_retain_explicit_reply_targets(self):
        context = _context()
        context.message_search.search_page.return_value = DiscordSearchPage((DiscordSearchMessage(
            900, 100, 1, "Member", "yes", "2026-09-16T00:00:00+00:00", reply_to_message_id=899,
        ),), 0, 5, 1, None, False, False)
        result = await search_discord_messages(context, {"query": "yes", "channel_id": 100})
        self.assertEqual(result["matches"][0]["reply_to_message_id"], 899)

    async def _history_review(self, contents, *, limit=None, author_id=None):
        context = _context()
        upper = discord.utils.time_snowflake(context.source_message.created_at)
        context.source_message.id = upper
        rows = tuple(
            DiscordSearchMessage(upper - index - 1, 100, 1 + index % 2,
                                 "Member", content, "2026-09-16T00:00:00+00:00")
            for index, content in enumerate(contents)
        )

        async def page(**kwargs):
            remaining = [row for row in rows if kwargs["after_id"] < row.message_id < kwargs["before_id"]]
            selected = tuple(remaining[:kwargs["limit"]])
            next_id = selected[-1].message_id if len(selected) == kwargs["limit"] else None
            return DiscordHistoryPage(selected, kwargs["before_id"], kwargs["after_id"], kwargs["limit"], next_id, next_id is None)

        context.message_search.history_page.side_effect = page
        arguments = {"channel_id": 100, "after": "2026-09-01"}
        if limit is not None:
            arguments["limit"] = limit
        if author_id is not None:
            arguments["author_id"] = author_id
        results = []
        for _ in range(len(rows) + 1):
            result = await read_discord_channel_history(context, arguments)
            self.assertNotIn("error", result)
            results.append(result)
            cursor = result["coverage"]["next_cursor"]
            if cursor is None:
                self.assertTrue(result["coverage"]["reached_requested_start"])
                break
            self.assertFalse(result["coverage"]["reached_requested_start"])
            arguments["cursor"] = cursor
        else:
            self.fail("History traversal did not finish")
        self.assertEqual(context.state.source_channels, {100})
        return results, rows

    async def test_larger_history_page_reads_same_evidence_with_fewer_tool_calls(self):
        from elbow_helper.agent.research.catalogue import discord_tools
        from elbow_helper.agent.plan.checker import valid_arguments

        tool = next(tool for tool in discord_tools() if tool.definition.name == "read_discord_channel_history")
        self.assertTrue(valid_arguments({"channel_id": 100, "after": "2026-09-01", "limit": 100}, tool.definition.parameters))
        small, _ = await self._history_review([f"Recruitment discussion {i}" for i in range(80)])
        large, rows = await self._history_review([f"Recruitment discussion {i}" for i in range(80)], limit=100)
        self.assertEqual(len(small), 4)
        self.assertLess(len(large), len(small))
        self.assertEqual([item for page in small for item in page["messages"]],
                         [item for page in large for item in page["messages"]])
        self.assertEqual([item["message_id"] for page in large for item in page["messages"]],
                         [row.message_id for row in rows])

    async def test_long_history_pages_keep_every_matching_message_and_continuation(self):
        from elbow_helper.agent.engine.budgets import MAX_TOOL_RESULT_CHARACTERS
        from elbow_helper.agent.engine.tool_call import bound_tool_result as _bound_tool_result

        for author_id in (None, 1):
            with self.subTest(author_id=author_id):
                results, rows = await self._history_review(['"\\\n' * 500 for _ in range(80)], author_id=author_id, limit=100)
                self.assertGreater(len(results), 1)
                actual = [item["message_id"] for result in results for item in result["messages"]]
                expected = [row.message_id for row in rows if author_id is None or row.author_id == author_id]
                self.assertEqual(actual, expected)
                self.assertEqual(len(set(actual)), len(actual))
                self.assertEqual(sum(result["coverage"]["scanned_messages_in_window"] for result in results), len(rows))
                for result in results:
                    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                    self.assertLess(len(encoded), MAX_TOOL_RESULT_CHARACTERS)
                    self.assertEqual(_bound_tool_result(encoded, MAX_TOOL_RESULT_CHARACTERS), encoded)

    async def test_twenty_results_are_valid_and_reach_discord(self):
        from elbow_helper.agent.research.catalogue import discord_tools
        from elbow_helper.agent.plan.checker import valid_arguments

        context = _context()
        tool = next(tool for tool in discord_tools() if tool.definition.name == "search_discord_messages")
        arguments = {"query": "joins", "channel_id": 100, "limit": 20}
        self.assertTrue(valid_arguments(arguments, tool.definition.parameters))
        context.message_search.search_page.return_value = DiscordSearchPage(
            (), 0, 20, 0, None, False, False,
        )
        await tool.handler(context, arguments)
        self.assertEqual(context.message_search.search_page.await_args.kwargs["limit"], 20)

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
            context.message_search.search_page.await_args.kwargs["channel_ids"],
            (100, 200),
        )
        self.assertEqual(context.state.source_channels, {100, 200})
        self.assertEqual(result["matches"], [])
        context.message_search.search_page.assert_awaited_once()

    async def test_multiple_channel_search_rejects_mixed_and_drops_inaccessible_scope(self):
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
        self.assertNotIn("error", denied)
        self.assertEqual(denied["missing_sources"], [{"channel_id": 200, "cause": "source_unavailable"}])
        self.assertEqual(context.message_search.search_page.await_args.kwargs["channel_ids"], (100,))
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

    async def test_out_of_scope_result_is_dropped_with_coverage(self):
        context = _context()
        context.message_search.search_page.return_value = DiscordSearchPage(
            (DiscordSearchMessage(12, 200, 1, "Member", "decision", ""),),
            0, 5, 1, None, False, False,
        )
        result = await search_discord_messages(
            context, {"query": "decision", "channel_id": 100},
        )
        self.assertNotIn("error", result)
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["coverage"]["out_of_scope_results_dropped"], 1)
        self.assertEqual(context.state.source_channels, {100})

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
            "elbow_helper.agent.research.threads.can_access_message_channel",
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
            "elbow_helper.agent.research.threads.can_access_message_channel",
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


class MessageContextResultContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_requested_record_is_marked_and_neighbours_are_only_context(self):
        from elbow_helper.agent.research.history import read_message_context
        messages = [SimpleNamespace(id=identifier, author=SimpleNamespace(id=101, display_name="Synthetic member"),
                                  created_at=datetime(2026, 9, 15, tzinfo=timezone.utc), content="Synthetic message",
                                  attachments=[], channel=SimpleNamespace(id=100)) for identifier in (300, 301, 302)]
        async def history(**kwargs):
            for message in messages:
                yield message
        channel = SimpleNamespace(id=100, name="synthetic", history=history)
        context = SimpleNamespace(private_pass=True, guild=SimpleNamespace(id=1), state=SimpleNamespace(source_channels=set()))
        with patch("elbow_helper.agent.research.history.accessible_message_channel", return_value=channel):
            result = await read_message_context(context, {"channel_id": 100, "message_id": 301})
        self.assertEqual([item["message_id"] for item in result["messages"]], [300, 301, 302])
        self.assertEqual([item["is_requested_message"] for item in result["messages"]], [False, True, False])
        messages.pop(1)
        with patch("elbow_helper.agent.research.history.accessible_message_channel", return_value=channel):
            result = await read_message_context(context, {"channel_id": 100, "message_id": 301})
        self.assertFalse(any(item["is_requested_message"] for item in result["messages"]))
