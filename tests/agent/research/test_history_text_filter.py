"""Unfiltered discussion chunks preserve paging, scope and durable scan counts."""

from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.infrastructure.discord.message_search import (
    DiscordHistoryPage, DiscordMessageSearch, DiscordSearchMessage,
)
from elbow_helper.configuration.roles import CORE
from elbow_helper.agent.engine.registry import build_agent_tools
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

    async def test_transport_retains_embed_text_and_attachment_names(self):
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
        result = await read_discord_channel_history(context, {
            "channel_id": 100, "after": "2026-09-01",
        })
        self.assertEqual(len(result["messages"]), 1)
        self.assertIn("Subject @Synthetic Member", result["messages"][0]["content"])
        self.assertIn("Synthetic Footer", result["messages"][0]["content"])
        self.assertIn("synthetic-transcript.html", result["messages"][0]["content"])
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

    async def test_author_selected_pages_do_not_skip_at_character_budget(self):
        context = _context()
        rows = history(context, [(1 + index % 2, "\"\\\n" * 500,
                                  ({"footer": {"text": "Straße"}},), ())
                                 for index in range(80)])
        arguments = {"channel_id": 100, "after": "2026-09-01", "limit": 100,
                     "author_id": 1}
        found, scanned = [], 0
        for _ in range(len(rows) + 1):
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

    async def test_complete_content_keeps_all_embed_text_without_transport_noise(self):
        context = _context()
        text = "Original text " + "x" * 1800
        history(context, [(1, text, ({
            "title": "Subject", "description": "Details", "author": {"name": "Speaker"},
            "footer": {"text": "Footer", "icon_url": "unused"},
            "fields": [{"name": "Field", "value": "Value", "inline": True}],
            "image": {"url": "unused", "placeholder": "x" * 16000},
        },), ("image.png",))])
        result = await read_discord_channel_history(context, {"channel_id": 100}, complete_content=True)
        row = result["messages"][0]
        self.assertEqual(row["content"], text)
        self.assertEqual(row["embeds"], [{"title": "Subject", "description": "Details",
            "author": {"name": "Speaker"}, "footer": {"text": "Footer"},
            "fields": [{"name": "Field", "value": "Value"}]}])
        self.assertEqual(row["attachment_names"], ["image.png"])
        self.assertFalse(row["attachment_contents_read"])
        self.assertTrue(row["content_complete"])

    def test_catalogue_offers_chunks_without_text_filters(self):
        tools = build_agent_tools()
        for name in ("read_discord_channel_history", "start_discord_history_job"):
            self.assertNotIn("text_filter", tools[name].definition.parameters["properties"])
            self.assertIn("meaning", tools[name].definition.usage_note)
