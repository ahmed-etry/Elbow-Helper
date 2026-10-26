"""Synthetic reply reports; no live Discord, member data, or model requests."""

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from elbow_helper.configuration import channels
from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.cog import AgentCog
from elbow_helper.features.agent.conversation.debug import OperatorDebugRecord
from elbow_helper.features.agent.conversation.transcripts import TranscriptArchive, _schema, _debug_schema
from elbow_helper.features.agent.models import AgentAttachment, AgentDelivery, AgentTurnState
from elbow_helper.features.agent.feedback import report_card
from elbow_helper.infrastructure.persistence import (
    SQLiteMigration, run_sqlite_migrations, sqlite_connection, sqlite_transaction,
)


def diagnostics():
    return OperatorDebugRecord(
        "Synthetic normalized question", answer="Synthetic generated but undelivered answer",
        tool_calls=[{"capability": "synthetic_lookup", "arguments": {"member_id": 42},
                     "result": {"private_result": "Synthetic evidence"}, "status": "completed"}],
        rounds=[{"round": 1, "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                 "content": "Synthetic internal draft", "outcome": "completed"}],
        usage={"rounds": 2, "prompt_tokens": 110, "completion_tokens": 22,
               "unknown_token_rounds": 0}, route="research", status="incomplete",
        error="Synthetic provider error", terminal_cause="provider_failed",
    )


def record_exchange(archive, *, request_id=1, reply_id=10, content="Synthetic delivered answer"):
    archive.record_request(message_id=request_id, guild_id=GUILD_ID, channel_id=100,
        root_message_id=request_id, member_id=42, created_at="2026-10-27T10:00:00+00:00",
        content="Synthetic question", replied_to_message_id=None, attachment_names=("question.png",))
    archive.record_reply(message_id=reply_id, request_message_id=request_id, content=content,
                         attachment_names=("answer.csv",))
    archive.record_debug(record=diagnostics(), request_message_id=request_id,
        guild_id=GUILD_ID, channel_id=100, root_message_id=request_id, member_id=42)


class ReplyReportStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "transcripts.sqlite3"
        self.archive = TranscriptArchive(self.path)
        record_exchange(self.archive)

    def report(self, **changes):
        values = dict(guild_id=GUILD_ID, channel_id=100, reply_id=10, reporter_id=42)
        return self.archive.record_reply_report(**{**values, **changes})

    def test_snapshot_reuses_delivered_text_arguments_and_accounting_and_survives_reopen(self):
        self.archive.record_reply(message_id=11, request_message_id=1, content="Synthetic second part")
        self.archive.record_reply(message_id=12, request_message_id=1, content="Synthetic confirmation", is_answer=False)
        report = self.report()
        self.assertEqual(report["request_message_id"], 1)
        self.assertEqual(report["channel_id"], 100)
        self.assertEqual(report["reply_message_id"], 10)
        self.assertEqual(report["reporter_id"], 42)
        self.assertIsNotNone(datetime.fromisoformat(report["reported_at"]).tzinfo)
        self.assertEqual(report["question"], "Synthetic question")
        self.assertEqual(report["delivered_answer"], "Synthetic delivered answer\nSynthetic second part")
        self.assertEqual(report["question_attachment_names"], ["question.png"])
        self.assertEqual(report["answer_attachment_names"], ["answer.csv"])
        self.assertEqual(report["reply_ids"], [10, 11])
        self.assertEqual(report["diagnostics"]["tool_calls"], [{"capability": "synthetic_lookup",
            "arguments": {"member_id": 42}, "status": "completed"}])
        self.assertEqual(report["diagnostics"]["usage"], diagnostics().usage)
        self.assertEqual(report["diagnostics"]["route"], "research")
        self.assertEqual(report["diagnostics"]["error"], "Synthetic provider error")
        self.assertEqual(report["diagnostics"]["terminal_cause"], "provider_failed")
        reopened = TranscriptArchive(self.path)
        with reopened.connect() as connection:
            row = connection.execute("SELECT * FROM reply_reports").fetchone()
        self.assertEqual(json.loads(row["evidence_json"])["delivered_answer"], report["delivered_answer"])
        self.assertNotIn("private_result", row["evidence_json"])
        self.assertNotIn("Synthetic internal draft", row["evidence_json"])
        self.assertNotIn("undelivered", row["evidence_json"])
        self.archive.replace_reply(message_id=10, request_message_id=1,
            previous_content="Synthetic delivered answer", content="Synthetic edited answer",
            attachment_names=("edited.png",))
        self.assertEqual(self.report(reporter_id=43)["answer_attachment_names"], ["edited.png"])
        self.assertEqual(json.loads(row["evidence_json"])["answer_attachment_names"], ["answer.csv"])

    def test_deduplication_is_durable_and_per_reporter_and_reply(self):
        self.assertIsNotNone(self.report())
        self.archive = TranscriptArchive(self.path)
        self.assertIsNone(self.report())
        self.assertIsNotNone(self.report(reporter_id=43))
        self.archive.record_reply(message_id=11, request_message_id=1, content="Synthetic second part")
        self.assertIsNotNone(self.report(reply_id=11))

    def test_only_exact_scoped_answers_can_be_reported(self):
        self.archive.record_reply(message_id=11, request_message_id=1, content="Synthetic preview", is_answer=False)
        for changes in ({"reply_id": 11}, {"reply_id": 999}, {"guild_id": 123}, {"channel_id": 123}):
            self.assertIsNone(self.report(**changes))

    def test_diagnostics_and_reports_survive_debug_retention(self):
        with self.archive.connect() as connection, sqlite_transaction(connection, immediate=True):
            connection.execute("UPDATE operator_debug_records SET created_at=?",
                ((datetime.now(timezone.utc) - timedelta(days=31)).isoformat(),))
        self.archive.prune_debug()
        self.assertEqual(self.report()["diagnostics"]["usage"], diagnostics().usage)
        with self.archive.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM operator_debug_records").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM reply_reports").fetchone()[0], 1)

    def test_request_boundary_failure_is_retained_with_engine_diagnostics(self):
        self.archive.record_request_failure(request_id=1, error="Synthetic Discord failure", terminal_cause="failed")
        summary = self.report()["diagnostics"]
        self.assertEqual(summary["request_error"], "Synthetic Discord failure")
        self.assertEqual(summary["request_terminal_cause"], "failed")
        self.assertEqual(summary["error"], "Synthetic provider error")
        self.assertEqual(summary["usage"], diagnostics().usage)

    def test_combined_file_answer_and_names_survive_later_preview_edits(self):
        self.archive.record_reply_delivery(request_id=1, reply_id=10,
            answer="Synthetic complete answer in file", attachment_names=("full-answer.txt",))
        self.archive.replace_reply(message_id=10, request_message_id=1,
            previous_content="Synthetic delivered answer", content="Synthetic changed preview")
        report = self.report()
        self.assertEqual(report["delivered_answer"], "Synthetic complete answer in file")
        self.assertEqual(report["answer_attachment_names"], ["full-answer.txt"])

    def test_version_two_migration_preserves_transcripts_and_backfills_available_diagnostics(self):
        legacy = Path(self.directory.name) / "legacy.sqlite3"
        with sqlite_connection(legacy) as connection:
            run_sqlite_migrations(connection, (
                SQLiteMigration(1, "legacy transcripts", _schema),
                SQLiteMigration(2, "legacy debug", _debug_schema),
            ), target_version=2)
            with sqlite_transaction(connection, immediate=True):
                connection.execute("INSERT INTO requests VALUES (1, ?, 100, 1, 42, '2026-10-01', 'Synthetic old question', NULL)", (GUILD_ID,))
                connection.execute("INSERT INTO replies(message_id, request_message_id, content) VALUES (10, 1, 'Synthetic old reply')")
                connection.execute("""INSERT INTO operator_debug_records (request_message_id, guild_id,
                    channel_id, root_message_id, member_id, created_at, record_json)
                    VALUES (1, ?, 100, 1, 42, '2026-10-01', ?)""", (GUILD_ID, json.dumps(asdict(diagnostics()))))
        migrated = TranscriptArchive(legacy)
        page, = migrated.read_page(guild_id=GUILD_ID, channel_id=100, root_message_id=1)
        self.assertEqual(page["content"], "Synthetic old question")
        self.assertEqual(page["replies"][0]["content"], "Synthetic old reply")
        with migrated.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            summary = json.loads(connection.execute("SELECT summary_json FROM request_diagnostics").fetchone()[0])
        self.assertEqual(summary["tool_calls"][0]["arguments"], {"member_id": 42})
        self.assertNotIn("result", summary["tool_calls"][0])


class _Channel:
    type = discord.ChannelType.text
    category_id = None
    overwrites = {}

    def __init__(self, identifier, guild, viewers):
        self.id, self.guild, self.viewers = identifier, guild, set(viewers)
        self.send = AsyncMock()

    def permissions_for(self, member):
        visible = member.id in self.viewers
        return SimpleNamespace(view_channel=visible, read_message_history=visible)


class ReplyReportReactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.archive = TranscriptArchive(Path(self.directory.name) / "transcripts.sqlite3")
        record_exchange(self.archive)
        self.member = SimpleNamespace(id=42, bot=False, roles=[SimpleNamespace(id=next(iter(CORE)))])
        self.outsider = SimpleNamespace(id=43, bot=False, roles=[])
        self.bot_member = SimpleNamespace(id=99, bot=True, roles=[])
        members = [self.member, self.outsider, self.bot_member]
        self.guild = SimpleNamespace(id=GUILD_ID, chunked=True, members=members,
            me=self.bot_member, default_role=SimpleNamespace(id=1), roles=[],
            get_member=lambda identifier: next((member for member in members if member.id == identifier), None))
        self.source = _Channel(100, self.guild, {42, 99})
        self.destination = _Channel(200, self.guild, {42, 99})
        self.channel_map = {100: self.source, 200: self.destination}
        self.guild.get_channel_or_thread = self.channel_map.get
        self.bot = SimpleNamespace(user=self.bot_member, agent_model=MagicMock(),
            get_guild=lambda identifier: self.guild if identifier == GUILD_ID else None,
            fetch_channel=AsyncMock(return_value=None))
        self.cog = AgentCog(self.bot, account_links=None, message_search=None,
                            roster_queries=None, transcript_archive=self.archive)
        self.payload = SimpleNamespace(guild_id=GUILD_ID, channel_id=100, message_id=10,
            user_id=42, member=self.member, emoji=SimpleNamespace(id=None, name="👎"))
        self.channel_patch = patch.object(channels, "AGENT_REPORTS", None)
        self.channel_patch.start()
        self.addCleanup(self.channel_patch.stop)

    def reports(self):
        with self.archive.connect() as connection:
            return connection.execute("SELECT * FROM reply_reports").fetchall()

    def test_raw_listener_is_registered_without_a_reaction_removal_listener(self):
        names = [name for name, _ in self.cog.get_listeners()]
        self.assertIn("on_raw_reaction_add", names)
        self.assertNotIn("on_raw_reaction_remove", names)

    async def test_disabled_channel_stores_and_logs_without_any_reply_or_send(self):
        with self.assertLogs("elbow_helper.features.agent.feedback", level="INFO"):
            await self.cog.on_raw_reaction_add(self.payload)
        self.assertEqual(len(self.reports()), 1)
        self.destination.send.assert_not_awaited()
        self.bot.fetch_channel.assert_not_awaited()
        self.cog.service._model.create_agent_session.assert_not_called()

    async def test_trigger_ignores_wrong_emoji_custom_emoji_guild_member_and_other_messages(self):
        for changes in (
            {"emoji": SimpleNamespace(id=None, name="👍")},
            {"emoji": SimpleNamespace(id=123, name="👎")},
            {"guild_id": None}, {"guild_id": 123}, {"user_id": 43}, {"user_id": 99},
            {"user_id": 999, "member": None}, {"message_id": 999}, {"channel_id": 999},
        ):
            with self.subTest(changes=changes):
                await self.cog.on_raw_reaction_add(SimpleNamespace(**{**vars(self.payload), **changes}))
        self.archive.record_reply(message_id=11, request_message_id=1, content="Synthetic preview", is_answer=False)
        self.payload.message_id = 11
        await self.cog.on_raw_reaction_add(self.payload)
        self.assertEqual(self.reports(), [])

    async def test_enabled_channel_posts_one_compact_card_and_duplicate_events_do_not_post(self):
        with patch.object(channels, "AGENT_REPORTS", 200):
            await asyncio.gather(*(self.cog.on_raw_reaction_add(self.payload) for _ in range(3)))
            await self.cog.on_raw_reaction_add(self.payload)
        self.assertEqual(len(self.reports()), 1)
        self.destination.send.assert_awaited_once()
        options = self.destination.send.await_args.kwargs
        card = options["embed"]
        self.assertEqual(card.title, "Agent reply report")
        fields = {field.name: field.value for field in card.fields}
        self.assertEqual(fields["Question"], "Synthetic question")
        self.assertEqual(fields["Answer"], "Synthetic delivered answer")
        self.assertEqual(fields["Rounds / tokens"], "2 / 110 + 22")
        self.assertEqual(fields["Reporter"], "<@42>")
        self.assertEqual(fields["Tools"], "synthetic_lookup")
        self.assertEqual(fields["Reply"], f"https://discord.com/channels/{GUILD_ID}/100/10")
        self.assertEqual(options["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())

    async def test_private_channel_with_extra_report_room_viewer_gets_only_link_and_request(self):
        self.destination.viewers.add(43)
        with patch.object(channels, "AGENT_REPORTS", 200):
            await self.cog.on_raw_reaction_add(self.payload)
        card = self.destination.send.await_args.kwargs["embed"]
        self.assertIsNone(card.title)
        self.assertEqual([field.name for field in card.fields], ["Reply", "Request"])
        self.assertNotIn("Synthetic", json.dumps(card.to_dict()))
        self.assertEqual(json.loads(self.reports()[0]["evidence_json"])["question"], "Synthetic question")

    async def test_private_thread_membership_is_checked_even_when_parent_is_readable(self):
        self.source.viewers.add(43)
        self.destination.viewers.add(43)
        thread = MagicMock(spec=discord.Thread)
        thread.id, thread.guild, thread.parent = 100, self.guild, self.source
        thread.is_private.return_value = True
        thread.permissions_for.side_effect = lambda actor: SimpleNamespace(
            view_channel=True, read_message_history=True, manage_threads=False)
        thread.fetch_members = AsyncMock(return_value=[SimpleNamespace(id=42)])
        self.channel_map[100] = thread
        with patch.object(channels, "AGENT_REPORTS", 200):
            await self.cog.on_raw_reaction_add(self.payload)
        self.assertEqual([field.name for field in self.destination.send.await_args.kwargs["embed"].fields],
                         ["Reply", "Request"])
        thread.fetch_members.assert_awaited_once()

    async def test_disclosure_error_posts_only_link_and_request(self):
        with (patch.object(channels, "AGENT_REPORTS", 200),
              patch("elbow_helper.features.agent.feedback.can_show", side_effect=RuntimeError("Synthetic permission failure")),
              self.assertLogs("elbow_helper.features.agent.feedback", level="ERROR")):
            await self.cog.on_raw_reaction_add(self.payload)
        self.assertEqual(len(self.destination.send.await_args.kwargs["embed"].fields), 2)

    async def test_missing_source_and_disclosure_failure_reduce_card_and_wrong_guild_never_sends(self):
        self.channel_map.pop(100)
        with patch.object(channels, "AGENT_REPORTS", 200):
            await self.cog.on_raw_reaction_add(self.payload)
        self.assertEqual(len(self.destination.send.await_args.kwargs["embed"].fields), 2)
        self.destination.send.reset_mock()
        self.destination.guild = SimpleNamespace(id=123)
        self.payload.user_id, self.payload.member = 43, self.outsider
        self.outsider.roles = self.member.roles
        with patch.object(channels, "AGENT_REPORTS", 200):
            await self.cog.on_raw_reaction_add(self.payload)
        self.destination.send.assert_not_awaited()

    async def test_reaction_waits_for_existing_request_and_pending_archive_writes(self):
        finished = asyncio.Event()
        self.cog._request_finished[1] = finished
        recording = asyncio.Event()
        loop = asyncio.get_running_loop()
        original = self.archive.reply_request_id

        def lookup(**values):
            loop.call_soon_threadsafe(recording.set)
            return original(**values)

        # Wait until the listener has resolved this exact request before completing it.
        with patch.object(self.archive, "reply_request_id", side_effect=lookup):
            task = asyncio.create_task(self.cog.on_raw_reaction_add(self.payload))
            await asyncio.wait_for(recording.wait(), 2)
            self.assertFalse(task.done())
            self.assertEqual(self.reports(), [])
            self.archive.enqueue_write(self.archive.record_reply, message_id=11, request_message_id=1,
                                       content="Synthetic final part")
            finished.set()
            await task
        self.assertEqual(json.loads(self.reports()[0]["evidence_json"])["reply_ids"], [10, 11])

    async def test_reaction_can_arrive_before_a_visible_answer_is_queued_for_archival(self):
        finished = asyncio.Event()
        self.cog._request_finished[1] = finished
        self.payload.message_id = 11
        state = AgentTurnState()
        state.reply_delivery.delivery.message_ids.append(11)
        self.cog._active_contexts[1] = SimpleNamespace(guild=self.guild,
            source_message=SimpleNamespace(channel=self.source), state=state)
        observed = asyncio.Event()
        loop = asyncio.get_running_loop()
        original = self.archive.reply_request_id

        def lookup(**values):
            loop.call_soon_threadsafe(observed.set)
            return original(**values)

        with patch.object(self.archive, "reply_request_id", side_effect=lookup):
            task = asyncio.create_task(self.cog.on_raw_reaction_add(self.payload))
            await asyncio.wait_for(observed.wait(), 2)
            self.archive.enqueue_write(self.archive.record_reply, message_id=11, request_message_id=1,
                                       content="Synthetic late archive")
            finished.set()
            await task
        self.assertEqual(self.reports()[0]["reply_message_id"], 11)

    async def test_send_failure_preserves_report_and_is_not_repeated_on_duplicate(self):
        self.destination.send.side_effect = OSError("Synthetic delivery failure")
        with patch.object(channels, "AGENT_REPORTS", 200):
            with self.assertLogs("elbow_helper.features.agent.feedback", level="ERROR"):
                await self.cog.on_raw_reaction_add(self.payload)
            await self.cog.on_raw_reaction_add(self.payload)
        self.assertEqual(len(self.reports()), 1)
        self.destination.send.assert_awaited_once()

    async def test_real_delivery_archives_answer_and_file_names_but_excludes_pure_preview(self):
        message = SimpleNamespace(id=1, guild=self.guild, channel=self.source, mentions=[],
            author=self.member, reply=AsyncMock(return_value=SimpleNamespace(id=20)))
        self.cog.action_runner = None
        attachment = AgentAttachment("synthetic.csv", b"synthetic")
        await self.cog.send_response(message, "Synthetic actual answer", None,
                                     (attachment,), delivery=AgentDelivery())
        await self.archive.drain()
        report = self.archive.record_reply_report(guild_id=GUILD_ID, channel_id=100,
                                                  reply_id=20, reporter_id=42)
        self.assertEqual(report["answer_attachment_names"], ["answer.csv", "synthetic.csv"])
        context = SimpleNamespace(state=AgentTurnState(proposed_changes=[object()]))
        # Exercise the same archive eligibility flag without generating a real preview.
        message.reply.return_value.id = 21
        with patch("elbow_helper.features.agent.delivery.build_reply_views", return_value=(None, None)):
            await self.cog._send_response_parts(message, "Synthetic preview", ["Synthetic preview"], {},
                None, None, discord.AllowedMentions.none(), AgentDelivery(), None, context, None)
        await self.archive.drain()
        self.assertIsNone(self.archive.reply_request_id(guild_id=GUILD_ID, channel_id=100, reply_id=21))

    def test_card_lengths_and_missing_diagnostics(self):
        report = self.archive.record_reply_report(guild_id=GUILD_ID, channel_id=100, reply_id=10, reporter_id=42)
        report.update(question="q" * 4_000, delivered_answer="a" * 8_000, diagnostics=None)
        card = report_card(report, disclose=True)
        self.assertLess(len(card), 6_000)
        self.assertTrue(all(len(field.value) <= 1_024 for field in card.fields))
        self.assertEqual({field.name: field.value for field in card.fields}["Tools"], "Unknown")
