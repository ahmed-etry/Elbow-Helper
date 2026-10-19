"""Synthetic full-text history filters preserve paging, scope and durable scan counts."""

from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from elbow_helper.discord.message_search import (
    DiscordHistoryPage, DiscordMessageSearch, DiscordSearchMessage,
)
from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.plan.format import capability_list
from elbow_helper.features.agent.research.history import discord_message_evidence, read_discord_channel_history
from elbow_helper.features.agent.research.repository import ResearchJobRepository
from elbow_helper.features.agent.research.tools import (
    continue_discord_research_job, start_discord_history_job,
)
from features.agent.research.test_agent_search import _context


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


class HistoryTextFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_retains_embed_text_and_attachment_names_for_filtering(self):
        context = _context()
        context.member.display_name = "Synthetic Member"
        upper = discord.utils.time_snowflake(context.source_message.created_at)
        context.source_message.id = upper
        http = SimpleNamespace(request=AsyncMock(return_value=[{
            "id": str(upper - 1), "channel_id": "100", "content": "",
            "author": {"id": "99", "username": "Synthetic Bot"},
            "timestamp": "2026-09-16T12:00:00+00:00",
            "message_reference": {"message_id": str(upper - 2), "channel_id": "100"},
            "embeds": [{"title": "Synthetic log", "author": {"name": "Subject <@1>"},
                        "footer": {"text": "Synthetic Footer"}}],
            "attachments": [{"filename": "synthetic-transcript.html"}],
        }]))
        context.message_search = DiscordMessageSearch(http)
        for text_filter in ("SUBJECT @SYNTHETIC MEMBER", "synthetic footer", "TRANSCRIPT.HTML"):
            with self.subTest(text_filter=text_filter):
                result = await read_discord_channel_history(context, {
                    "channel_id": 100, "after": "2026-09-01", "text_filter": text_filter,
                })
                self.assertEqual(len(result["messages"]), 1)
                self.assertIn("Subject @Synthetic Member", result["messages"][0]["content"])
                self.assertEqual(result["coverage"]["scanned_count"], 1)
                self.assertEqual(result["coverage"]["matched_count"], 1)
                self.assertEqual(result["messages"][0]["reply_to_message_id"], upper - 2)
                self.assertEqual(result["coverage"]["order"], "newest_first")

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

    async def test_filter_scans_full_text_and_pages_without_skipping_matches_at_the_budget(self):
        context = _context()
        rows = history(context, [(1 + index % 2, "\"\\\n" * 500,
                                  ({"footer": {"text": "Straße"}},), ())
                                 for index in range(80)])
        arguments = {"channel_id": 100, "after": "2026-09-01", "limit": 100,
                     "author_id": 1, "text_filter": "STRASSE"}
        found, scanned = [], 0
        for _ in range(10):
            result = await read_discord_channel_history(context, arguments)
            self.assertNotIn("error", result)
            found.extend(item["message_id"] for item in result["messages"])
            scanned += result["coverage"]["scanned_count"]
            self.assertEqual(result["coverage"]["matched_count"], len(result["messages"]))
            cursor = result["coverage"]["next_cursor"]
            if cursor is None:
                break
            arguments["cursor"] = cursor
        else:
            self.fail("Synthetic filtered history did not finish")
        self.assertEqual(found, [row.message_id for row in rows if row.author_id == 1])
        self.assertEqual(scanned, len(rows))
        self.assertGreater(context.message_search.history_page.await_count, 1)

    async def test_nonmatching_page_keeps_cursor_and_filter_is_bound_to_it(self):
        context = _context()
        history(context, [(1, "Other", (), ()), (1, "Synthetic target", (), ())])
        arguments = {"channel_id": 100, "after": "2026-09-01", "limit": 1,
                     "text_filter": "target"}
        first = await read_discord_channel_history(context, arguments)
        self.assertEqual(first["messages"], [])
        self.assertEqual(first["coverage"]["scanned_count"], 1)
        arguments["cursor"] = first["coverage"]["next_cursor"]
        changed = await read_discord_channel_history(context, {**arguments, "text_filter": "other"})
        self.assertIn("does not match", changed["error"])
        second = await read_discord_channel_history(context, {**arguments, "text_filter": "TARGET"})
        self.assertEqual(len(second["messages"]), 1)
        self.assertEqual(context.message_search.history_page.await_count, 2)

    async def test_filter_and_cumulative_counts_survive_job_restart(self):
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
                "text_filter": "TARGET",
            })
            self.assertEqual(started["query"], "TARGET")
            first = await continue_discord_research_job(context, {"job_id": started["job_id"]})
            self.assertEqual(first["new_messages"], [])
            context.research_jobs = ResearchJobRepository(path)
            second = await continue_discord_research_job(context, {"job_id": started["job_id"]})
            self.assertEqual(len(second["new_messages"]), 1)
            self.assertEqual(second["new_messages"][0]["reply_to_message_id"], 100)
            last = await continue_discord_research_job(context, {"job_id": started["job_id"]})
            self.assertEqual(last["status"], "completed")
            self.assertEqual(last["coverage"]["scanned_count"], 2)
            self.assertEqual(last["coverage"]["matched_count"], 1)
            self.assertEqual(last["retained_message_count"], 1)

    async def test_invalid_filters_do_not_scan(self):
        context = _context()
        for value in ("", " ", 7, "x" * 1025):
            with self.subTest(value=value):
                result = await read_discord_channel_history(context, {
                    "channel_id": 100, "after": "2026-09-01", "text_filter": value,
                })
                self.assertEqual(result["error"],
                                 "Use a non-empty text filter of up to 1,024 characters.")
        context.message_search.history_page.assert_not_awaited()

    def test_catalogue_explains_embed_search_and_both_history_filters(self):
        tools = build_agent_tools()
        lines = capability_list(tools).splitlines()
        for name in ("read_discord_channel_history", "start_discord_history_job"):
            line = next(line for line in lines if line.startswith(name + ":"))
            self.assertIn("text_filter:str", line)
            self.assertIn("case-insensitively", line)
        self.assertIn("Search misses embeds; filter history for bot logs",
                      tools["search_discord_messages"].definition.usage_note)
