"""Unfiltered discussion chunks preserve paging, scope and durable scan counts."""

from tests.temp_directory import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.infrastructure.discord.message_search import DiscordHistoryPage, DiscordSearchMessage
from elbow_helper.configuration.roles import CORE
from elbow_helper.agent.research.history import discord_message_evidence, read_discord_channel_history
from elbow_helper.agent.research.repository import ResearchJobRepository
from elbow_helper.agent.research.tools import (
    continue_discord_research_job, start_discord_history_job,
)
from agent.research.test_agent_search import _context


def history(context, records, *, reply_to_message_id=None):
    upper = discord.utils.time_snowflake(context.source_message.created_at)
    context.source_message.id = upper
    rows = tuple(DiscordSearchMessage(
        upper - index - 1, 100, author, "Synthetic Bot", content,
        "2026-09-16T12:00:00+00:00", embeds, attachments,
        reply_to_message_id=reply_to_message_id,
    ) for index, (author, content, embeds, attachments) in enumerate(records))

    async def page(**arguments):
        selected = tuple(row for row in rows
                         if arguments["after_id"] < row.message_id < arguments["before_id"])
        selected = selected[:arguments["limit"]]
        cursor = selected[-1].message_id if len(selected) == arguments["limit"] else None
        return DiscordHistoryPage(selected, arguments["before_id"], arguments["after_id"],
                                  arguments["limit"], cursor, cursor is None)

    context.message_search.history_page.side_effect = page
    return rows


class HistoryChunkTests(unittest.IsolatedAsyncioTestCase):
    async def test_batched_pages_return_every_message_for_semantic_review(self):
        context = _context()
        rows = history(context, [(1, "Other", (), ()) for _ in range(4)]
                       + [(1, "Synthetic target", (), ())])
        result = await read_discord_channel_history(context, {
            "channel_id": 100, "after": "2026-09-01", "limit": 2, "pages": 4,
        })
        self.assertEqual([row["message_id"] for row in result["messages"]], [row.message_id for row in rows])
        self.assertEqual(result["coverage"]["scanned_count"], 5)
        self.assertTrue(result["coverage"]["reached_requested_start"])
        self.assertEqual(context.message_search.history_page.await_count, 3)

    async def test_batched_budget_continuation_keeps_every_message_exactly_once(self):
        context = _context()
        rows = history(context, [(1, "target " + '"\\' * 500, (), ()) for _ in range(40)])
        arguments = {"channel_id": 100, "after": "2026-09-01", "limit": 4, "pages": 8}
        found = []
        for _ in range(50):
            result = await read_discord_channel_history(context, arguments)
            found.extend(row["message_id"] for row in result["messages"])
            cursor = result["coverage"]["next_cursor"]
            if cursor is None:
                break
            arguments["cursor"] = cursor
        else:
            self.fail("Batched history failed to advance")
        self.assertEqual(found, [row.message_id for row in rows])

    async def test_batched_access_loss_discards_all_earlier_messages(self):
        context = _context()
        history(context, [(1, "Sensitive", (), ()), (1, "Other", (), ())])
        channel = context.guild.get_channel_or_thread(100)
        with patch("elbow_helper.agent.research.history.accessible_message_channel",
                   AsyncMock(side_effect=[channel, channel, None])):
            result = await read_discord_channel_history(context, {
                "channel_id": 100, "after": "2026-09-01", "limit": 1, "pages": 2,
            })
        self.assertIn("error", result)
        self.assertNotIn("messages", result)


    def test_message_context_keeps_reply_metadata_without_treating_forwards_as_replies(self):
        from datetime import datetime, timezone
        message = SimpleNamespace(
            id=300, author=SimpleNamespace(id=1, display_name="Member"), content="yes",
            channel=SimpleNamespace(id=100), created_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
            attachments=[], embeds=[], stickers=[],
        )
        for channel, kind, expected in (
            (100, discord.MessageReferenceType.default, 200),
            (100, discord.MessageReferenceType.forward, None),
            (999, discord.MessageReferenceType.default, None),
        ):
            with self.subTest(channel=channel, kind=kind):
                message.reference = SimpleNamespace(message_id=200, channel_id=channel, type=kind)
                result = discord_message_evidence(10, message)
                self.assertEqual(result.get("reply_to_message_id"), expected)


    async def test_unknown_start_and_request_local_cursor_read_older_messages(self):
        context = _context()
        rows = history(context, [(1, "Opening", (), ()), (2, "Follow-up", (), ())])
        first = await read_discord_channel_history(context, {"channel_id": 100, "limit": 1})
        second = await read_discord_channel_history(context, {
            "channel_id": 100, "limit": 1, "cursor": first["coverage"]["next_cursor"],
        })
        self.assertEqual(first["messages"][0]["message_id"], rows[0].message_id)
        self.assertEqual(second["messages"][0]["message_id"], rows[1].message_id)
        self.assertIsNone(second["coverage"]["requested_after"])
        self.assertEqual(context.message_search.history_page.await_args.kwargs["after_id"], 0)

    async def test_all_messages_and_counts_survive_job_restart(self):
        with TemporaryDirectory() as directory:
            context = _context()
            context.source_message.channel = context.guild.get_channel_or_thread(100)
            context.member.roles = [SimpleNamespace(id=next(iter(CORE)))]
            history(context, [(1, "Other", (), ()),
                              (1, "", ({"footer": {"text": "Synthetic target"}},), ())], reply_to_message_id=100)
            path = Path(directory) / "synthetic.sqlite"
            context.research_jobs = ResearchJobRepository(path)
            context.conversation_root_id = 500
            started = await start_discord_history_job(context, {
                "channel_id": 100, "after": "2026-09-01", "page_size": 1,
            })
            self.assertEqual(started["query"], "")
            first = await continue_discord_research_job(context, {"job_id": started["job_id"]})
            self.assertEqual(len(first["new_messages"]), 1)
            context.research_jobs = ResearchJobRepository(path)
            second = await continue_discord_research_job(context, {"job_id": started["job_id"]})
            self.assertEqual(len(second["new_messages"]), 1)
            self.assertEqual(second["new_messages"][0]["reply_to_message_id"], 100)
            last = await continue_discord_research_job(context, {"job_id": started["job_id"]})
            self.assertEqual(last["status"], "completed")
            self.assertEqual(last["coverage"]["scanned_count"], 2)
            self.assertEqual(last["coverage"]["matched_count"], 2)
            self.assertEqual(last["retained_message_count"], 2)

    async def test_legacy_text_selection_is_rejected_without_scanning(self):
        context = _context()
        result = await read_discord_channel_history(context, {
            "channel_id": 100, "text_filter": "topic",
        })
        self.assertIn("without a text filter", result["error"])
        context.message_search.history_page.assert_not_awaited()
