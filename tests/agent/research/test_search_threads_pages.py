"""Search parent scope, consecutive pages, and request budget regressions."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from elbow_helper.agent.engine.budgets import ContextBudget
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.steps import PlanRunner
from elbow_helper.agent.families.reference_reads import TOOLS, conversation_evidence
from elbow_helper.agent.research.search import search_discord_messages
from elbow_helper.infrastructure.discord.message_search import DiscordSearchMessage, DiscordSearchPage
from agent.engine.test_agent_plan_flow import _context as runner_context
from agent.research.test_agent_search import _context


def message(identifier, channel_id=100, *, content="Synthetic evidence.", author_id=1):
    return DiscordSearchMessage(identifier, channel_id, author_id, "Synthetic member", content, "")


def thread(context, *, parent_id=100, private=False):
    channel = Mock(spec=discord.Thread)
    channel.id = 300
    channel.name = "Synthetic discussion"
    channel.guild = context.guild
    channel.parent_id = parent_id
    channel.parent = context.guild.get_channel_or_thread(parent_id)
    channel.is_private.return_value = private
    channel.permissions_for.return_value = SimpleNamespace(
        view_channel=True, read_message_history=True, manage_threads=False,
    )
    channel.fetch_member = AsyncMock(return_value=SimpleNamespace(id=context.member.id))
    return channel


def paged_search(context, *, content="Synthetic evidence."):
    async def page(**arguments):
        offset = arguments["offset"]
        return DiscordSearchPage(
            (message(900 - offset, content=content), message(899 - offset, content=content)),
            offset, arguments["limit"], 100, offset + 25, False, False,
        )
    context.message_search.search_page = AsyncMock(side_effect=page)


class ThreadScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_thread_and_forum_post_under_requested_parent_are_kept(self):
        for parent_kind in (discord.TextChannel, discord.ForumChannel):
            with self.subTest(parent_kind=parent_kind):
                context = _context()
                parent = Mock(spec=parent_kind)
                parent.id, parent.guild = 100, context.guild
                parent.name = "Synthetic parent"
                parent.category_id = None
                parent.permissions_for.return_value = SimpleNamespace(
                    view_channel=True, read_message_history=True,
                )
                context.guild.channels[0] = parent
                child = thread(context)
                context.guild.channels.append(child)
                context.message_search.search_page.return_value = DiscordSearchPage(
                    (message(900, 300), message(899)), 0, 25, 2, None, False, False,
                )
                result = await search_discord_messages(context, {"channel_ids": [100]})
                self.assertNotIn("error", result)
                self.assertEqual([row["channel_id"] for row in result["matches"]], [300, 100])
                self.assertEqual(context.state.source_channels, {100, 300})

    async def test_uncached_thread_is_resolved_through_existing_access_check(self):
        context = _context()
        context.bot = SimpleNamespace(fetch_channel=AsyncMock(return_value=thread(context)))
        context.message_search.search_page.return_value = DiscordSearchPage(
            (message(900, 300),), 0, 25, 1, None, False, False,
        )
        result = await search_discord_messages(context, {"channel_id": 100})
        self.assertEqual([row["channel_id"] for row in result["matches"]], [300])
        context.bot.fetch_channel.assert_awaited_with(300)

    async def test_unrelated_channel_and_thread_are_dropped_without_losing_valid_rows(self):
        context = _context()
        context.guild.channels.append(thread(context, parent_id=200))
        context.message_search.search_page.return_value = DiscordSearchPage(
            (message(900, 200), message(899, 300), message(898)),
            0, 25, 100, 25, False, False,
        )
        result = await search_discord_messages(context, {"channel_ids": [100]})
        self.assertNotIn("error", result)
        self.assertEqual([row["message_id"] for row in result["matches"]], [898])
        self.assertEqual(result["coverage"]["out_of_scope_results_dropped"], 2)
        self.assertEqual(result["coverage"]["next_offset"], 25)
        self.assertEqual(context.state.source_channels, {100})

    async def test_parent_scope_does_not_bypass_private_thread_membership(self):
        for allowed in (True, False):
            with self.subTest(allowed=allowed):
                context = _context()
                child = thread(context, private=True)
                if not allowed:
                    child.fetch_member.side_effect = discord.NotFound(
                        SimpleNamespace(status=404, reason="Missing"), "Synthetic missing membership",
                    )
                context.guild.channels.append(child)
                context.message_search.search_page.return_value = DiscordSearchPage(
                    (message(900, 300), message(899)), 0, 25, 2, None, False, False,
                )
                result = await search_discord_messages(context, {"channel_ids": [100]})
                self.assertNotIn("error", result)
                self.assertEqual([row["message_id"] for row in result["matches"]],
                                 [900, 899] if allowed else [899])
                self.assertEqual(300 in context.state.source_channels, allowed)
                child.fetch_member.assert_awaited()

    async def test_unavailable_thread_does_not_fail_the_page(self):
        context = _context()
        context.bot = SimpleNamespace(fetch_channel=AsyncMock(side_effect=discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Synthetic unavailable thread",
        )))
        context.message_search.search_page.return_value = DiscordSearchPage(
            (message(900, 300), message(899)), 0, 25, 2, None, False, False,
        )
        result = await search_discord_messages(context, {"channel_ids": [100]})
        self.assertNotIn("error", result)
        self.assertEqual([row["message_id"] for row in result["matches"]], [899])
        self.assertEqual(context.state.source_channels, {100})

    async def test_author_and_time_scope_violations_are_dropped(self):
        context = _context()
        context.message_search.search_page.return_value = DiscordSearchPage(
            (message(900, author_id=2), message(1001), message(899)),
            0, 25, 3, None, False, False,
        )
        result = await search_discord_messages(context, {"channel_ids": [100], "author_id": 1})
        self.assertNotIn("error", result)
        self.assertEqual([row["message_id"] for row in result["matches"]], [899])
        self.assertEqual(result["coverage"]["out_of_scope_results_dropped"], 2)


class SearchPagingTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_and_author_mentions_read_three_pages_and_resume_after_last(self):
        for mode in ("search", "author_mentions"):
            with self.subTest(mode=mode):
                context = _context()
                paged_search(context)
                arguments = {"mode": mode, "channel_ids": [100], "pages": 3}
                result = await conversation_evidence(context, arguments)
                self.assertEqual([row["message_id"] for row in result["rows"]],
                                 [900, 899, 875, 874, 850, 849])
                self.assertEqual(result["coverage"]["pages_scanned"], 3)
                self.assertEqual(result["coverage"]["returned_accessible_matches"], 6)
                self.assertEqual(result["coverage"]["next_offset"], 75)
                self.assertEqual(result["coverage"]["order"], "newest_first")
                context.source_message.id = 2000
                await conversation_evidence(context, {**arguments, "pages": 1,
                    "source_cursor": result["source_next_cursor"]})
                self.assertEqual([call.kwargs["offset"] for call in
                                  context.message_search.search_page.await_args_list], [0, 25, 50, 75])
                self.assertTrue(all(call.kwargs["max_id"] == 1000 for call in
                                    context.message_search.search_page.await_args_list))

    async def test_character_budget_stops_mid_page_without_skipping_omitted_row(self):
        context = _context()
        paged_search(context, content="x" * 600)
        with patch("elbow_helper.agent.research.search.COMPLETE_HISTORY_CHARACTER_BUDGET", 2500):
            result = await conversation_evidence(context, {
                "mode": "search", "channel_id": 100, "pages": 3,
            })
        self.assertEqual([row["message_id"] for row in result["rows"]], [900, 899])
        self.assertEqual(context.message_search.search_page.await_count, 2)
        self.assertTrue(result["coverage"]["stopped_at_message_budget"])
        self.assertEqual(result["coverage"]["next_offset"], 25)
        await conversation_evidence(context, {"mode": "search", "channel_id": 100,
            "source_cursor": result["source_next_cursor"]})
        self.assertEqual(context.message_search.search_page.await_args.kwargs["offset"], 25)

    async def test_partial_page_cursor_includes_filtered_rows_before_budget_stop(self):
        context = _context()
        context.message_search.search_page.return_value = DiscordSearchPage(
            (message(900, 200), message(899, content="x" * 600), message(898, content="x" * 600)),
            0, 25, 3, None, False, False,
        )
        with patch("elbow_helper.agent.research.search.COMPLETE_HISTORY_CHARACTER_BUDGET", 1500):
            result = await conversation_evidence(context, {
                "mode": "search", "channel_ids": [100], "pages": 3,
            })
        self.assertEqual([row["message_id"] for row in result["rows"]], [899])
        self.assertEqual(result["coverage"]["next_offset"], 2)
        self.assertFalse(result["coverage"]["reached_current_indexed_end"])
        self.assertEqual(result["coverage"]["returned_indexed_matches"], 2)

    async def test_empty_filtered_page_still_reads_next_page(self):
        context = _context()
        context.message_search.search_page.side_effect = (
            DiscordSearchPage((message(900, 200),), 0, 25, 2, 25, False, False),
            DiscordSearchPage((message(899),), 25, 25, 2, None, False, False),
        )
        result = await search_discord_messages(context, {"channel_ids": [100], "pages": 3})
        self.assertEqual([row["message_id"] for row in result["matches"]], [899])
        self.assertEqual(result["coverage"]["pages_scanned"], 2)
        self.assertIsNone(result["coverage"]["next_cursor"])
        self.assertEqual(result["coverage"]["out_of_scope_results_dropped"], 1)

    async def test_access_loss_on_later_page_discards_earlier_pages(self):
        context = _context()
        async def page(**arguments):
            if arguments["offset"]:
                context.guild.channels[0].permissions_for = lambda _: SimpleNamespace(
                    view_channel=False, read_message_history=False,
                )
            return DiscordSearchPage((message(900),), arguments["offset"], 25, 2, 25, False, False)
        context.message_search.search_page.side_effect = page
        result = await search_discord_messages(context, {"channel_ids": [100], "pages": 3})
        self.assertIn("error", result)
        self.assertNotIn("matches", result)
        self.assertEqual(context.message_search.search_page.await_count, 2)

    async def test_invalid_page_counts_do_not_call_discord(self):
        for pages in (0, 9, True, "3"):
            context = _context()
            result = await search_discord_messages(context, {"channel_id": 100, "pages": pages})
            self.assertIn("error", result)
            context.message_search.search_page.assert_not_awaited()

    def runner(self):
        context = runner_context()
        context = replace(context, message_search=SimpleNamespace())
        # These synthetic messages precede the frozen request boundary.
        context.source_message.id = 1000
        context.source_message.channel.id = 100
        paged_search(context, content="x" * 600)
        tool = next(tool for tool in TOOLS if tool.definition.name == "read_conversation_evidence")
        return PlanRunner(service=AgentService(None), context=context,
            registry={tool.definition.name: tool}, command_capabilities={},
            budget=ContextBudget(None, 8192, 0), rounder=SimpleNamespace(social_route=False))

    async def run_search(self, runner, identifier="search"):
        step = {"id": identifier, "capability": "read_conversation_evidence",
                "arguments": {"mode": "search", "channel_id": 100, "pages": 3}, "depends_on": []}
        return (await runner.run({"steps": [step]}))[identifier]

    async def test_each_page_counts_toward_request_lookup_cap(self):
        runner = self.runner()
        with patch("elbow_helper.agent.engine.budgets.MAX_TOOL_CALLS", 2):
            result = await self.run_search(runner)
            self.assertEqual(result["coverage"]["pages_scanned"], 2)
            self.assertEqual(result["coverage"]["next_offset"], 50)
            self.assertTrue(result["coverage"]["stopped_at_request_budget"])
            self.assertEqual(runner.tool_calls, 2)
            self.assertEqual(runner.context.message_search.search_page.await_count, 2)
            rejected = await self.run_search(runner, "over_cap")
        self.assertEqual(rejected["error"], "The request reached its lookup limit.")

    async def test_shared_evidence_budget_includes_prior_reads(self):
        runner = self.runner()
        runner.evidence_characters = 3000
        with patch("elbow_helper.agent.engine.budgets.MAX_DISCUSSION_EVIDENCE_CHARACTERS", 5000):
            result = await self.run_search(runner)
        self.assertTrue(result["coverage"]["stopped_at_message_budget"])
        self.assertEqual(result["coverage"]["next_offset"], 25)
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(runner.tool_calls, 2)
        self.assertLessEqual(runner.evidence_characters, 5000)

    async def test_complete_three_page_read_charges_three_lookups(self):
        runner = self.runner()
        result = await self.run_search(runner)
        self.assertEqual(result["coverage"]["pages_scanned"], 3)
        self.assertEqual(result["coverage"]["next_offset"], 75)
        self.assertEqual(len(result["rows"]), 6)
        self.assertEqual(runner.tool_calls, 3)
