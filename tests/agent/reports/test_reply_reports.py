"""Synthetic reply reports; no live Discord, member data, or model requests."""

import asyncio
from datetime import datetime
import json
from pathlib import Path
from tests.temp_directory import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from elbow_helper.configuration.guild import GUILD_ID, OWNER_DISCORD_ID
from elbow_helper.configuration.roles import CORE
from elbow_helper.agent.cog import AgentCog
from elbow_helper.agent.conversation.debug import OperatorDebugRecord
from elbow_helper.agent.conversation.transcripts import TranscriptArchive
from elbow_helper.agent.engine.models import AgentAttachment, AgentDelivery, AgentTurnState


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
        self.owner = SimpleNamespace(id=OWNER_DISCORD_ID, send=AsyncMock())
        self.bot = SimpleNamespace(user=self.bot_member, agent_model=MagicMock(),
            get_user=MagicMock(return_value=self.owner), fetch_user=AsyncMock(return_value=self.owner),
            get_guild=lambda identifier: self.guild if identifier == GUILD_ID else None,
            fetch_channel=AsyncMock(return_value=None))
        self.cog = AgentCog(self.bot, account_links=None, message_search=None,
                            roster_queries=None, transcript_archive=self.archive)
        self.payload = SimpleNamespace(guild_id=GUILD_ID, channel_id=100, message_id=10,
            user_id=42, member=self.member, emoji=SimpleNamespace(id=None, name="👎"))

    def reports(self):
        with self.archive.connect() as connection:
            return connection.execute("SELECT * FROM reply_reports").fetchall()


    async def test_owner_dm_has_full_card_and_duplicates_do_not_send(self):
        await asyncio.gather(*(self.cog.on_raw_reaction_add(self.payload) for _ in range(3)))
        await self.cog.on_raw_reaction_add(self.payload)
        self.assertEqual(len(self.reports()), 1)
        self.owner.send.assert_awaited_once()
        options = self.owner.send.await_args.kwargs
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

    async def test_uncached_owner_is_fetched_without_resolving_source_channel(self):
        self.bot.get_user.return_value = None
        self.channel_map.clear()
        await self.cog.on_raw_reaction_add(self.payload)
        self.bot.get_user.assert_called_once_with(OWNER_DISCORD_ID)
        self.bot.fetch_user.assert_awaited_once_with(OWNER_DISCORD_ID)
        self.bot.fetch_channel.assert_not_awaited()
        self.source.send.assert_not_awaited()
        self.destination.send.assert_not_awaited()
        fields = {field.name: field.value for field in self.owner.send.await_args.kwargs['embed'].fields}
        self.assertEqual(fields['Question'], 'Synthetic question')
        self.assertEqual(fields['Answer'], 'Synthetic delivered answer')
        self.cog.service._model.create_agent_session.assert_not_called()

    async def test_fetch_failure_logs_and_preserves_saved_report(self):
        self.bot.get_user.return_value = None
        self.bot.fetch_user.side_effect = OSError('Synthetic fetch failure')
        with self.assertLogs('elbow_helper.agent.reports.feedback', level='ERROR') as logs:
            await self.cog.on_raw_reaction_add(self.payload)
        self.assertIn('owner DM failed', '\n'.join(logs.output))
        self.assertEqual(len(self.reports()), 1)
        self.owner.send.assert_not_awaited()

    async def test_unrelated_reactions_and_ineligible_reporters_are_ignored(self):
        for changes in ({'guild_id': 123}, {'user_id': 99, 'member': self.bot_member},
                        {'user_id': 43, 'member': self.outsider}, {'message_id': 999},
                        {'emoji': SimpleNamespace(id=None, name='\U0001f44d')}):
            with self.subTest(changes=changes):
                payload = SimpleNamespace(**{**vars(self.payload), **changes})
                await self.cog.on_raw_reaction_add(payload)
        self.assertEqual(self.reports(), [])
        self.owner.send.assert_not_awaited()

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
        self.owner.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'),
                                                       'Synthetic blocked DM')
        with self.assertLogs('elbow_helper.agent.reports.feedback', level='ERROR') as logs:
            await self.cog.on_raw_reaction_add(self.payload)
        self.assertIn('owner DM failed', '\n'.join(logs.output))
        await self.cog.on_raw_reaction_add(self.payload)
        self.assertEqual(len(self.reports()), 1)
        self.owner.send.assert_awaited_once()

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
        with patch("elbow_helper.agent.conversation.delivery.build_reply_views", return_value=(None, None)):
            await self.cog._send_response_parts(message, "Synthetic preview", ["Synthetic preview"], {},
                None, None, discord.AllowedMentions.none(), AgentDelivery(), None, context, None)
        await self.archive.drain()
        self.assertIsNone(self.archive.reply_request_id(guild_id=GUILD_ID, channel_id=100, reply_id=21))
