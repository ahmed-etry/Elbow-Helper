from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timezone
from contextlib import nullcontext
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import discord
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch
from unittest.mock import ANY

from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import CORE, LEAD_PLUS
from elbow_helper.configuration.channels import OVERSEEING_TERRACE
from elbow_helper.discord.interactions import DEFAULT_FAILURE_MESSAGE
from elbow_helper.discord.message_search import DiscordMessageSearch
from elbow_helper.features.agent.access import ACCESS_LEAD_PLUS, AgentAccessLost
from elbow_helper.features.agent.cog import CoreAgent
from elbow_helper.features.agent.delivery import _delivery_nonce
from elbow_helper.features.agent.message_content import message_text
from elbow_helper.features.agent.conversation.state import ConversationTurn
from elbow_helper.features.agent.models import AgentDelivery, AgentTurnState
from elbow_helper.features.agent.reports.roles import RoleAccountReport
from elbow_helper.features.agent.conversation.instructions import WorkingState
from elbow_helper.features.agent.conversation.transcripts import TranscriptArchive
from elbow_helper.features.agent.conversation.repository import ConversationRepository
from elbow_helper.features.agent.conversation.codec import decode_conversation
from elbow_helper.features.agent.conversation.persistence import ConversationPersistence
from elbow_helper.features.agent.conversation.context import build_history_checkpoint
from elbow_helper.features.agent.service import CoreAgentService
from elbow_helper.infrastructure.ai.client import DeepSeekTextClient
from elbow_helper.features.member_lifecycle.queries import MemberLifecycleQueries


class _Member:
    def __init__(self, member_id: int, role_ids: tuple[int, ...]):
        self.id = member_id
        self.bot = False
        self.display_name = f"Member {member_id}"
        self.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]


def _message(*, author: _Member, bot_id: int, content: str):
    channel = SimpleNamespace(
        id=100,
        permissions_for=lambda actor: SimpleNamespace(
            view_channel=True, read_message_history=True,
        ),
    )
    guild = SimpleNamespace(
        id=GUILD_ID,
        me=SimpleNamespace(id=bot_id),
        get_member=lambda member_id: author if member_id == author.id else None,
    )
    return SimpleNamespace(
        id=1,
        channel=channel,
        reference=None,
        author=author,
        guild=guild,
        raw_mentions=[bot_id],
        content=content,
    )


class CoreAgentCogTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_generation_errors_send_existing_failure_without_retry(self):
        for error_type in (AttributeError, KeyError, IndexError, ZeroDivisionError):
            with self.subTest(error=error_type.__name__):
                self.setUp()
                member = _Member(42, (next(iter(CORE)),))
                make_message = self._real_handler_scenario(member)
                message = make_message(member, 1, "<@999> check #rec-room and #rec-support")
                self.cog.service.answer.side_effect = error_type("private diagnostic")
                with (
                    patch("elbow_helper.features.agent.cog.discord.Member", _Member),
                    self.assertLogs("elbow_helper.features.agent.cog", level="ERROR") as logs,
                ):
                    await self.cog.on_message(message)
                self.cog.service.answer.assert_awaited_once()
                message.reply.assert_awaited_once()
                self.assertEqual(message.reply.await_args.args, (DEFAULT_FAILURE_MESSAGE,))
                self.assertTrue(any("request=1" in line for line in logs.output))
                self.assertFalse(self.cog._tasks)
                conversation = next(value for _, value in self.cog._conversations.entries())
                self.assertEqual(conversation.pending, 0)
                self.assertFalse(conversation.turns)

    async def test_generation_timeout_sends_failure_and_logs_request(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        message = make_message(member, 1, "<@999> check #rec-room and #rec-support")

        async def stalled_generation(**kwargs):
            await asyncio.Event().wait()

        self.cog.service.answer.side_effect = stalled_generation
        with (
            patch("elbow_helper.features.agent.cog.discord.Member", _Member),
            patch("elbow_helper.features.agent.cog.AGENT_REQUEST_TIMEOUT_SECONDS", 0.01),
            self.assertLogs("elbow_helper.features.agent.cog", level="WARNING") as logs,
        ):
            await self.cog.on_message(message)
        self.cog.service.answer.assert_awaited_once()
        message.reply.assert_awaited_once()
        self.assertEqual(message.reply.await_args.args, (DEFAULT_FAILURE_MESSAGE,))
        self.assertTrue(any("Agent request timed out: request=1" in line for line in logs.output))
        self.assertFalse(self.cog._tasks)

    async def test_failure_delivery_errors_are_logged_without_retry(self):
        for error in (
            discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"),
            OSError("Connection lost"),
        ):
            with self.subTest(error=type(error).__name__):
                message = SimpleNamespace(id=1, channel=SimpleNamespace(id=100), reply=AsyncMock(side_effect=error))
                with self.assertLogs("elbow_helper.features.agent.delivery", level="WARNING") as logs:
                    await self.cog._send_failure(message)
                message.reply.assert_awaited_once()
                self.assertTrue(any("request=1 channel=100" in line for line in logs.output))

    async def test_two_channel_research_reaches_discord_through_real_orchestration(self):
        for recover_final_call, ticket_state in ((False, None), (True, None), (False, "disappearing"), (False, "stale")):
            with self.subTest(recover_final_call=recover_final_call, ticket_state=ticket_state):
                self.setUp()
                member = _Member(42, (next(iter(CORE)),))
                make_message = self._real_handler_scenario(member)
                message = make_message(member, 1000, (
                    "<@999> check #rec-room and #rec-support, summarize recruitment "
                    "activity, and explain what should have been posted"
                ))
                channels = {100: message.channel}
                for channel_id, name in ((200, "rec-room"), (300, "rec-support")):
                    channels[channel_id] = SimpleNamespace(
                        id=channel_id, name=name, guild=message.guild,
                        permissions_for=message.channel.permissions_for,
                    )
                message.channel.name = "agent-room"
                message.guild.channels = list(channels.values())
                message.guild.threads = []
                message.guild.get_channel_or_thread = channels.get
                if ticket_state is not None:
                    channels[OVERSEEING_TERRACE] = SimpleNamespace(
                        id=OVERSEEING_TERRACE, guild=message.guild,
                        permissions_for=message.channel.permissions_for,
                    )
                    message.guild.members = [member]
                    ticket = SimpleNamespace(
                        id=400, guild=message.guild,
                        permissions_for=message.channel.permissions_for,
                    )
                    missing = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")
                    self.bot.fetch_channel = AsyncMock(side_effect=missing)
                    if ticket_state == "disappearing":
                        checks = 0

                        def channel_lookup(channel_id):
                            nonlocal checks
                            if channel_id == 400:
                                checks += 1
                                return ticket if checks <= 4 else None
                            return channels.get(channel_id)

                        message.guild.get_channel_or_thread = channel_lookup
                    self.cog.member_lifecycle_queries = MemberLifecycleQueries(lambda: {
                        "members": {str(member.id): {
                            "platform": "private platform", "joined_at_iso": "2026-09-15T12:00:00+00:00",
                            "left": False,
                        }},
                        "last_seen": {str(member.id): {
                            "channel_id": 400, "ts_iso": "2026-09-15T12:00:00+00:00",
                        }},
                    })
                http = SimpleNamespace(request=AsyncMock(return_value={
                    "messages": [[{
                        "id": str(channel_id + 1), "channel_id": str(channel_id),
                        "author": {"id": "42", "username": "Recruiter"},
                        "content": content, "timestamp": "2026-09-15T12:00:00+00:00",
                    }] for channel_id, content in (
                        (200, "Two applicants joined the recruitment discussion."),
                        (300, "Please post the recruitment follow-up here."),
                    )],
                    "total_results": 2,
                }))
                self.cog.message_search = DiscordMessageSearch(http)

                def response(*, name=None, arguments=None, content=None):
                    calls = None if name is None else [{
                        "id": name, "function": {
                            "name": name, "arguments": json.dumps(arguments),
                        },
                    }]
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message={
                            "role": "assistant", "content": content,
                            "tool_calls": calls, "reasoning_content": "retained reasoning",
                        })],
                        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100),
                    )

                answer = (
                    "Two applicants joined the discussion in #rec-room. "
                    f"https://discord.com/channels/{GUILD_ID}/200/201\n"
                    "#rec-support requested a recruitment follow-up. "
                    f"https://discord.com/channels/{GUILD_ID}/300/301\n"
                    "Suggested post: a follow-up covering the applicants' next steps."
                )
                responses = [
                    response(name="discover_agent_tools", arguments={"groups": ["discord_research", "member_cases"]}),
                    response(name="find_discord_channels", arguments={"query": "rec-"}),
                    response(name="search_discord_messages", arguments={
                        "channel_ids": [200, 300], "limit": 20,
                    }),
                ]
                if ticket_state is not None:
                    batch = responses[-1].choices[0].message["tool_calls"]
                    batch[:0] = [
                        response(name=name, arguments={}).choices[0].message["tool_calls"][0]
                        for name in ("read_member_lifecycle", "read_active_recruitment_trials")
                    ]
                if recover_final_call:
                    responses.append(response(content=(
                        '<||DSML|| calls><||DSML|| invoke name="find_discord_channels">'
                        '<||DSML|| parameter name="query" string="true">rec-'
                        '</||DSML|| parameter></||DSML|| invoke></||DSML|| calls>'
                    )))
                responses.append(response(content=answer))
                create = AsyncMock(side_effect=responses)
                transport = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                with (
                    patch("elbow_helper.infrastructure.ai.client.AsyncOpenAI", return_value=transport),
                    patch("elbow_helper.features.agent.cog.discord.Member", _Member),
                    patch("elbow_helper.features.agent.service.MAX_MODEL_ROUNDS", 4),
                ):
                    self.cog.service = CoreAgentService(DeepSeekTextClient("test-key"))
                    await self.cog.on_message(message)

                message.reply.assert_awaited_once()
                self.assertEqual(message.reply.await_args.args, (answer,))
                self.assertEqual(create.await_count, 5 if recover_final_call else 4)
                http.request.assert_awaited_once()
                params = http.request.await_args.kwargs["params"]
                self.assertEqual([int(value) for key, value in params if key == "channel_id"], [200, 300])
                final_request = create.await_args.kwargs
                self.assertEqual(final_request["tool_choice"], "none")
                results = [item["content"] for item in final_request["messages"] if item.get("role") == "tool"]
                self.assertTrue(any("Two applicants" in item and "follow-up here" in item for item in results))
                if ticket_state == "disappearing":
                    self.bot.fetch_channel.assert_awaited_once_with(400)
                    self.assertNotIn("private platform", str(final_request))
                    self.assertEqual([json.loads(item)["error"] for item in results[-3:-1]], ["That lookup failed."] * 2)
                elif ticket_state == "stale":
                    self.bot.fetch_channel.assert_not_awaited()
                    lifecycle = json.loads(results[-3])
                    self.assertEqual(lifecycle["tracked_current_member_count"], 1)
                    self.assertEqual(lifecycle["members"][0]["platform"], "private platform")
                    self.assertIsNone(lifecycle["members"][0]["last_seen_channel_id"])
                    self.assertIsNone(lifecycle["members"][0]["last_seen_at"])
                if recover_final_call:
                    self.assertIn("not executed", results[-1])
                conversation = self.cog._conversations.find(GUILD_ID, 100, 2000)
                self.assertTrue(conversation.turns[0].record.delivery_complete)
                expected_sources = {100, 200, 300}
                if ticket_state == "stale":
                    expected_sources.add(OVERSEEING_TERRACE)
                self.assertEqual(conversation.turns[0].source_channels, frozenset(expected_sources))
                if ticket_state == "disappearing":
                    self.assertFalse(conversation.reports)

    async def test_evidence_access_loss_sends_existing_failure_when_request_channel_remains_accessible(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        message = make_message(member, 1, "<@999> check recruitment")
        self.cog.service.answer.side_effect = AgentAccessLost("private source unavailable")
        with (
            patch("elbow_helper.features.agent.cog.discord.Member", _Member),
            self.assertLogs("elbow_helper.features.agent.cog", level="WARNING"),
        ):
            await self.cog.on_message(message)
        message.reply.assert_awaited_once()
        self.assertEqual(message.reply.await_args.args, (DEFAULT_FAILURE_MESSAGE,))
        self.cog.service.answer.assert_awaited_once()

    async def test_restricted_report_is_hidden_without_erasure_and_returns_after_role_restore(self):
        core_role = next(iter(CORE))
        lead_role = next(iter(LEAD_PLUS))
        lead = _Member(42, (core_role, lead_role))
        core_only = _Member(43, (core_role,))
        make_message = self._real_handler_scenario(lead, core_only)
        report = RoleAccountReport("restricted", "2026-09-17", (), ())
        observed = []

        async def answer(**kwargs):
            state = kwargs["context"].state
            observed.append(tuple(state.reports))
            if len(observed) == 1:
                state.reports[report.report_id] = report
                state.report_sources[report.report_id] = frozenset(
                    state.source_channels
                )
                state.report_access_requirements[report.report_id] = frozenset(
                    {ACCESS_LEAD_PLUS}
                )
                state.required_access.add(ACCESS_LEAD_PLUS)
            return "Answer"

        self.cog.service.answer.side_effect = answer
        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            await self.cog.on_message(make_message(lead, 1, "<@999> restricted"))
            await self.cog.on_message(make_message(core_only, 2, "continue", reply_to=1001))
            await self.cog.on_message(make_message(lead, 3, "continue", reply_to=1002))

        self.assertEqual(observed, [(), (), ("restricted",)])
        conversation = self.cog._conversations.find(GUILD_ID, 100, 1003)
        self.assertIn("restricted", conversation.reports)
        self.assertEqual(
            conversation.report_access_requirements["restricted"],
            frozenset({ACCESS_LEAD_PLUS}),
        )

    async def test_restart_continues_from_saved_reply_and_expiry_keeps_transcript(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        with TemporaryDirectory() as directory:
            archive = TranscriptArchive(Path(directory) / "transcripts.sqlite3")
            repository = ConversationRepository(Path(directory) / "agent.sqlite3")
            self.cog.transcript_archive = archive
            self.cog.persistence = ConversationPersistence(repository)
            with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
                await self.cog.on_message(make_message(member, 1, "<@999> first request"))
            self.cog = CoreAgent(self.bot, account_links=object(), clan_health=object(),
                                 message_search=object(), roster_queries=object(), transcript_archive=archive,
                                 persistence=ConversationPersistence(ConversationRepository(repository.path)))
            make_message = self._real_handler_scenario(member)
            await self.cog.cog_load()
            try:
                with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
                    await self.cog.on_message(make_message(member, 2, "continue", reply_to=1001))
                request = self.cog.service.answer.await_args.kwargs
                self.assertIn("first request", request["conversation_history"])
                self.assertEqual(request["context"].history[0].record.request_message_id, 1)
                rows = archive.read_page(guild_id=GUILD_ID, channel_id=100, root_message_id=1)
                self.assertEqual([row["content"] for row in rows], ["<@999> first request", "continue"])
                self.assertEqual([row["replies"][0]["content"] for row in rows], ["Answer", "Answer"])
                repository.prune(now=time.time() + 21601)
                self.assertEqual(repository.load_active(now=time.time() + 21601, limit=128), ())
                self.assertEqual(len(archive.read_page(guild_id=GUILD_ID, channel_id=100, root_message_id=1)), 2)
            finally:
                self.cog.cog_unload()
                await asyncio.gather(self.cog._cleanup_task, return_exceptions=True)

    async def test_reply_lazily_restores_a_valid_snapshot_after_memory_eviction(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        with TemporaryDirectory() as directory:
            repository = ConversationRepository(Path(directory) / "agent.sqlite3")
            self.cog.persistence = ConversationPersistence(repository)
            with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
                await self.cog.on_message(
                    make_message(member, 1, "<@999> first request"),
                )
            with patch(
                "elbow_helper.features.agent.conversation.state.MAX_CONVERSATIONS", 1,
            ):
                self.cog._conversations.create(GUILD_ID, 100, 99)
                self.assertIsNone(
                    self.cog._conversations.find(GUILD_ID, 100, 1001),
                )
                with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
                    await self.cog.on_message(
                        make_message(member, 2, "continue", reply_to=1001),
                    )

            request = self.cog.service.answer.await_args.kwargs
            self.assertIn("first request", request["conversation_history"])
            snapshot = repository.find_reply(
                GUILD_ID, 100, 1002, now=time.time(),
            )
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.revision, 2)

    async def test_partial_reply_archives_only_confirmed_text(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        with TemporaryDirectory() as directory:
            archive = TranscriptArchive(Path(directory) / "transcripts.sqlite3")
            self.cog.transcript_archive = archive
            self.cog.service.answer.return_value = "x" * 2100
            message = make_message(member, 1, "<@999> question")
            message.channel.send.side_effect = OSError("send failed")
            with patch("elbow_helper.features.agent.cog.discord.Member", _Member), self.assertLogs("elbow_helper.features.agent.cog", level="WARNING"):
                await self.cog.on_message(message)
            rows = archive.read_page(guild_id=GUILD_ID, channel_id=100, root_message_id=1)
            self.assertEqual(len(rows[0]["replies"]), 1)
            self.assertEqual(rows[0]["replies"][0]["content"], "x" * 2000)

    def _real_handler_scenario(self, *members):
        """Exercise the Discord handler with only network/model boundaries faked."""
        channel = SimpleNamespace(
            id=100,
            typing=lambda: nullcontext(),
            permissions_for=lambda actor: SimpleNamespace(
                view_channel=True, read_message_history=True,
            ),
            send=AsyncMock(return_value=SimpleNamespace(id=9000)),
        )
        by_id = {member.id: member for member in members}
        guild = SimpleNamespace(
            id=GUILD_ID,
            name="Brown Elbow",
            me=SimpleNamespace(id=999),
            get_member=by_id.get,
            get_channel_or_thread=lambda value: channel if value == channel.id else None,
        )
        channel.guild = guild
        self.cog._answer = CoreAgent._answer.__get__(self.cog, CoreAgent)
        self.cog._build_local_context = AsyncMock(return_value="Nearby discussion")
        self.cog._resolve_referenced_message = AsyncMock(return_value=None)
        self.cog.service.answer = AsyncMock(return_value="Answer")

        def make_message(member, message_id, content, *, reply_to=None):
            message = _message(author=member, bot_id=999, content=content)
            message.id = message_id
            message.guild = guild
            message.channel = channel
            message.mentions = []
            message.created_at = datetime(2026, 9, 16, tzinfo=timezone.utc)
            message.reply = AsyncMock(return_value=SimpleNamespace(id=1000 + message_id))
            if reply_to is not None:
                message.raw_mentions = []
                message.reference = SimpleNamespace(channel_id=channel.id, message_id=reply_to)
            return message

        return make_message

    async def test_another_core_member_can_continue_with_their_own_identity(self):
        role_id = next(iter(CORE))
        first_member = _Member(42, (role_id,))
        second_member = _Member(43, (role_id,))
        make_message = self._real_handler_scenario(first_member, second_member)
        first = make_message(first_member, 1, "<@999> review BEC")
        second = make_message(second_member, 2, "what about my accounts?", reply_to=1001)

        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            await self.cog.on_message(first)
            await self.cog.on_message(second)

        request = self.cog.service.answer.await_args.kwargs
        self.assertIs(request["context"].member, second_member)
        self.assertIs(request["context"].roster_queries, self.cog.roster_queries)
        self.assertIs(request["context"].cwl_queries, self.cog.cwl_queries)
        self.assertIs(request["context"].war_queries, self.cog.war_queries)
        self.assertEqual(request["context"].attachment_sources, (second,))
        self.assertIn("review BEC", request["conversation_history"])
        self.assertEqual(len(request["context"].history), 1)
        self.assertEqual(request["context"].history[0].record.request_message_id, 1)
        conversation = self.cog._conversations.find(GUILD_ID, 100, 1002)
        self.assertEqual(len(conversation.turns), 2)

    async def test_concurrent_duplicate_event_generates_and_delivers_only_once(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        message = make_message(member, 1, "<@999> help with this")
        started, release = asyncio.Event(), asyncio.Event()
        async def answer(**kwargs):
            started.set()
            await release.wait()
            return "Answer"
        self.cog.service.answer.side_effect = answer
        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            first = asyncio.create_task(self.cog.on_message(message))
            await started.wait()
            duplicate = asyncio.create_task(self.cog.on_message(message))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, duplicate)
        self.cog.service.answer.assert_awaited_once()
        message.reply.assert_awaited_once()

    async def test_an_unrelated_mention_does_not_replay_another_conversation(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        first = make_message(member, 1, "<@999> review BEC")
        second = make_message(member, 2, "<@999> different task")

        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            await self.cog.on_message(first)
            await self.cog.on_message(second)

        self.assertEqual(self.cog.service.answer.await_args.kwargs["conversation_history"], "")
        self.assertEqual(self.cog.service.answer.await_args.kwargs["context"].history, ())
        self.assertIsNot(
            self.cog._conversations.find(GUILD_ID, 100, 1001),
            self.cog._conversations.find(GUILD_ID, 100, 1002),
        )

    async def test_core_access_is_rechecked_after_waiting_for_conversation_lock(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        conversation = self.cog._conversations.create(GUILD_ID, 100, 90)
        self.cog._conversations.register_reply(conversation, 91)
        message = make_message(member, 1, "continue", reply_to=91)

        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            async with conversation.lock:
                task = asyncio.create_task(self.cog.on_message(message))
                try:
                    await asyncio.sleep(0)
                    self.assertEqual(conversation.pending, 1)
                    member.roles = []
                except BaseException:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise
            await asyncio.wait_for(task, timeout=1)

        self.cog.service.answer.assert_not_awaited()
        message.reply.assert_not_awaited()
        self.assertEqual(conversation.pending, 0)
        self.assertFalse(self.cog._tasks)

    async def test_core_access_lost_during_generation_prevents_delivery(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        message = make_message(member, 1, "<@999> review BEC")

        async def revoke(**kwargs):
            member.roles = []
            return "Private result"

        self.cog.service.answer.side_effect = revoke
        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            await self.cog.on_message(message)

        message.reply.assert_not_awaited()
        message.channel.send.assert_not_awaited()
        self.assertFalse(self.cog._tasks)

    async def test_unload_cancels_work_and_releases_conversation_state(self):
        member = _Member(42, (next(iter(CORE)),))
        message = _message(author=member, bot_id=999, content="<@999> hello")
        started = asyncio.Event()

        async def wait_for_cancellation(*args):
            started.set()
            await asyncio.Event().wait()

        self.cog._answer.side_effect = wait_for_cancellation
        conversation = self.cog._conversations.create(GUILD_ID, 100, 90)
        self.cog._conversations.register_reply(conversation, 91)
        message.reference = SimpleNamespace(channel_id=100, message_id=91)
        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            task = asyncio.create_task(self.cog.on_message(message))
            try:
                await asyncio.wait_for(started.wait(), timeout=1)
                self.cog.cog_unload()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(conversation.pending, 0)
        self.assertFalse(conversation.lock.locked())
        self.assertFalse(self.cog._tasks)

    async def test_full_pending_queue_does_not_start_another_model_request(self):
        member = _Member(42, (next(iter(CORE)),))
        message = _message(author=member, bot_id=999, content="<@999> hello")
        self.cog._send_failure = AsyncMock()
        with (
            patch("elbow_helper.features.agent.cog.discord.Member", _Member),
            patch("elbow_helper.features.agent.cog.MAX_PENDING_REQUESTS", 0),
        ):
            await self.cog.on_message(message)

        self.cog._answer.assert_not_awaited()
        self.cog._send_failure.assert_awaited_once_with(message)
        self.assertFalse(self.cog._tasks)

    async def test_every_delivered_chunk_can_continue_the_conversation(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        message = make_message(member, 1, "<@999> explain")
        conversation = self.cog._conversations.create(GUILD_ID, 100, 1)

        await self.cog._send_response(message, "x" * 2100, None, conversation=conversation)

        self.assertIs(self.cog._conversations.find(GUILD_ID, 100, 1001), conversation)
        self.assertIs(self.cog._conversations.find(GUILD_ID, 100, 9000), conversation)
        self.assertEqual(len(message.reply.await_args.args[0]), 2000)
        self.assertEqual(len(message.channel.send.await_args.args[0]), 100)
        for call in (message.reply.await_args, message.channel.send.await_args):
            self.assertFalse(call.kwargs["allowed_mentions"].everyone)
            self.assertFalse(call.kwargs["allowed_mentions"].roles)

    async def test_partial_delivery_retains_confirmed_text_and_complete_record(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        message = make_message(member, 1, "<@999> explain")
        self.cog.service.answer.return_value = "x" * 2100
        message.channel.send.side_effect = OSError("Simulated delivery failure")

        with (
            patch("elbow_helper.features.agent.cog.discord.Member", _Member),
            self.assertLogs("elbow_helper.features.agent.cog", level="WARNING"),
        ):
            await self.cog.on_message(message)

        conversation = self.cog._conversations.find(GUILD_ID, 100, 1001)
        self.assertIsNotNone(conversation)
        self.assertEqual(len(conversation.turns), 1)
        self.assertEqual(json.loads(conversation.turns[0].text)["answer"], "x" * 2000)
        record = conversation.turns[0].record
        self.assertEqual(record.generated_answer, "x" * 2100)
        self.assertEqual(record.delivered_answer, "x" * 2000)
        self.assertEqual(record.reply_ids, (1001,))
        self.assertFalse(record.delivery_complete)
        self.assertTrue(record.delivery_unknown)
        self.assertEqual(record.uncertain_nonce, _delivery_nonce(1, 1))
        self.assertEqual(record.attempted_nonces, (
            _delivery_nonce(1, 0), _delivery_nonce(1, 1),
        ))
        self.assertEqual(message.reply.await_count, 1)
        self.assertEqual(conversation.pending, 0)

    async def test_unknown_first_send_is_retained_without_duplicate_failure(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        message = make_message(member, 1, "<@999> explain")
        message.reply.side_effect = OSError("connection lost after send")
        self.cog._send_failure = AsyncMock()

        with TemporaryDirectory() as directory:
            repository = ConversationRepository(Path(directory) / "agent.sqlite3")
            self.cog.persistence = ConversationPersistence(repository)
            with (
                patch("elbow_helper.features.agent.cog.discord.Member", _Member),
                self.assertLogs(
                    "elbow_helper.features.agent.cog", level="WARNING",
                ),
            ):
                await self.cog.on_message(message)

            conversation = dict(self.cog._conversations.entries())[1]
            record = conversation.turns[0].record
            self.assertEqual(record.reply_ids, ())
            self.assertEqual(record.delivered_answer, "")
            self.assertFalse(record.delivery_complete)
            self.assertTrue(record.delivery_unknown)
            self.assertEqual(record.uncertain_nonce, _delivery_nonce(1, 0))
            self.assertEqual(
                record.attempted_nonces, (_delivery_nonce(1, 0),),
            )
            self.cog._send_failure.assert_not_awaited()
            message.reply.assert_awaited_once()

            snapshots = repository.load_active(now=time.time(), limit=128)
            self.assertEqual(len(snapshots), 1)
            restored = decode_conversation(snapshots[0])
            self.assertTrue(
                restored.turns[0].record.delivery_unknown
            )

    async def test_uncertain_send_reconciles_by_nonce_without_resending(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        message = make_message(member, 1, "<@999> explain")
        message.reply.side_effect = OSError("connection lost after send")
        nonce = _delivery_nonce(1, 0)
        delivered = SimpleNamespace(
            id=777, nonce=nonce,
            author=SimpleNamespace(id=self.cog.bot.user.id),
        )

        async def history(*, limit):
            self.assertEqual(limit, 25)
            yield delivered

        message.channel.history = history
        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            await self.cog.on_message(message)

        message.reply.assert_awaited_once()
        self.assertEqual(message.reply.await_args.kwargs["nonce"], nonce)
        conversation = self.cog._conversations.find(GUILD_ID, 100, 777)
        self.assertIsNotNone(conversation)
        record = conversation.turns[0].record
        self.assertTrue(record.delivery_complete)
        self.assertFalse(record.delivery_unknown)
        self.assertEqual(record.reply_ids, (777,))

    async def test_duplicate_event_reconciles_retained_unknown_send(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        message = make_message(member, 1, "<@999> explain")
        message.reply.side_effect = OSError("connection lost after send")
        self.cog._send_failure = AsyncMock()

        with TemporaryDirectory() as directory:
            repository = ConversationRepository(Path(directory) / "agent.sqlite3")
            self.cog.persistence = ConversationPersistence(repository)
            with (
                patch("elbow_helper.features.agent.cog.discord.Member", _Member),
                self.assertLogs(
                    "elbow_helper.features.agent.cog", level="WARNING",
                ),
            ):
                await self.cog.on_message(message)

            nonce = _delivery_nonce(1, 0)
            delivered = SimpleNamespace(
                id=777, nonce=nonce,
                author=SimpleNamespace(id=self.cog.bot.user.id),
            )

            async def history(*, limit):
                self.assertEqual(limit, 25)
                yield delivered

            message.channel.history = history
            with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
                await self.cog.on_message(message)

            self.cog.service.answer.assert_awaited_once()
            message.reply.assert_awaited_once()
            self.cog._send_failure.assert_not_awaited()
            conversation = self.cog._conversations.find(GUILD_ID, 100, 777)
            self.assertIsNotNone(conversation)
            record = conversation.turns[0].record
            self.assertEqual(record.delivered_answer, "Answer")
            self.assertEqual(record.reply_ids, (777,))
            self.assertTrue(record.delivery_complete)
            self.assertFalse(record.delivery_unknown)
            snapshot = repository.find_reply(
                GUILD_ID, 100, 777, now=time.time(),
            )
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.revision, 2)
            self.assertFalse(
                decode_conversation(snapshot).turns[0].record.delivery_unknown,
            )

    async def test_cancellation_during_nonce_reconciliation_remains_unknown(self):
        delivery = AgentDelivery()
        started = asyncio.Event()

        async def sender(*_, **__):
            raise OSError("uncertain")

        async def history(*, limit):
            self.assertEqual(limit, 25)
            started.set()
            await asyncio.Event().wait()
            yield  # pragma: no cover

        channel = SimpleNamespace(history=history)
        task = asyncio.create_task(self.cog._send_delivery_part(
            sender, channel, 123, delivery, "answer",
        ))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(delivery.unknown)
        self.assertEqual(delivery.uncertain_nonce, 123)
        self.assertEqual(delivery.attempted_nonces, [123])

    async def test_access_lost_after_first_chunk_stops_further_delivery(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        message = make_message(member, 1, "<@999> explain")
        self.cog.service.answer.return_value = "x" * 2100

        async def send_first(*args, **kwargs):
            member.roles = []
            return SimpleNamespace(id=1001)

        message.reply.side_effect = send_first
        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            await self.cog.on_message(message)

        message.channel.send.assert_not_awaited()
        message.reply.assert_awaited_once()
        conversation = self.cog._conversations.find(GUILD_ID, 100, 1001)
        self.assertFalse(conversation.turns[0].record.delivery_complete)

    async def test_partial_answer_keeps_generated_report_available_to_followups(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        message = make_message(member, 1, "<@999> build a report")
        report = RoleAccountReport(report_id="report", created_at="2026-09-16", roles=(), members=())

        async def generate(**kwargs):
            state = kwargs["context"].state
            state.reports[report.report_id] = report
            state.report_sources[report.report_id] = frozenset(
                state.source_channels
            )
            state.report_access_requirements[report.report_id] = frozenset()
            return "x" * 2100

        self.cog.service.answer.side_effect = generate
        message.channel.send.side_effect = OSError("Simulated delivery failure")
        with (
            patch("elbow_helper.features.agent.cog.discord.Member", _Member),
            self.assertLogs("elbow_helper.features.agent.cog", level="WARNING"),
        ):
            await self.cog.on_message(message)
        conversation = self.cog._conversations.find(GUILD_ID, 100, 1001)
        self.assertIs(conversation.reports["report"], report)

        self.cog.service.answer.side_effect = None
        self.cog.service.answer.return_value = "The report is ready."
        followup = make_message(member, 2, "use that report", reply_to=1001)
        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            await self.cog.on_message(followup)
        self.assertIs(self.cog.service.answer.await_args.kwargs["context"].state.reports["report"], report)


    async def test_deleted_replied_to_message_does_not_raise(self):
        message = SimpleNamespace(
            reference=SimpleNamespace(channel_id=100, message_id=91, resolved=None),
            channel=SimpleNamespace(
                id=100,
                fetch_message=AsyncMock(side_effect=discord.NotFound(
                    SimpleNamespace(status=404, reason="Not Found"),
                    {"code": 10008, "message": "Unknown Message"},
                )),
            ),
        )

        self.assertIsNone(await self.cog._resolve_referenced_message(message))
        message.channel.fetch_message.assert_awaited_once_with(91)

    async def test_sent_reply_and_earlier_evidence_reach_the_next_real_handler_turn(self):
        member = _Member(42, (next(iter(CORE)),))
        channel = SimpleNamespace(id=100, typing=lambda: nullcontext(),
                                  permissions_for=lambda member: SimpleNamespace(view_channel=True, read_message_history=True))
        guild = SimpleNamespace(id=GUILD_ID, name="Brown Elbow", me=member, get_member=lambda value: member,
                                get_channel_or_thread=lambda value: channel if value == 100 else None)
        channel.guild = guild
        self.cog._answer = CoreAgent._answer.__get__(self.cog, CoreAgent)
        self.cog._build_local_context = AsyncMock(return_value="Nearby discussion")
        self.cog._resolve_referenced_message = AsyncMock(return_value=None)
        async def answer(**kwargs):
            kwargs["context"].state.evidence.append("The linked account is #2PP")
            return "The account is in BE4."
        self.cog.service.answer = AsyncMock(side_effect=answer)
        first = _message(author=member, bot_id=999, content="<@999> find his account")
        second = _message(author=member, bot_id=999, content="where is that account?")
        for index, message in enumerate((first, second), start=1):
            message.id = index
            message.guild = guild
            message.channel = channel
            message.mentions = []
            message.created_at = datetime.now(timezone.utc)
            message.reply = AsyncMock(return_value=SimpleNamespace(id=100 + index))
        second.raw_mentions = []
        second.reference = SimpleNamespace(channel_id=100, message_id=101)
        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            await self.cog.on_message(first)
            await self.cog.on_message(second)
        self.assertEqual(self.cog.service.answer.await_count, 2)
        history = self.cog.service.answer.await_args.kwargs["conversation_history"]
        self.assertIn("find his account", history)
        self.assertIn("#2PP", history)
        self.assertIn("The account is in BE4.", history)
        self.assertIs(self.cog._conversations.find(GUILD_ID, 100, 101), self.cog._conversations.find(GUILD_ID, 100, 102))

    async def test_reply_without_ping_continues_only_a_registered_agent_conversation(self):
        member = _Member(42, (next(iter(CORE)),))
        conversation = self.cog._conversations.create(GUILD_ID, 100, 90)
        self.cog._conversations.register_reply(conversation, 91)
        message = _message(author=member, bot_id=999, content="what about his other account?")
        message.raw_mentions = []
        message.reference = SimpleNamespace(channel_id=100, message_id=91)
        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            await self.cog.on_message(message)
        self.cog._answer.assert_awaited_once_with(message, member, message.content, conversation)
        message.reference.message_id = 92
        self.assertFalse(self.cog._is_agent_request(message))

    async def test_followups_wait_for_earlier_work_instead_of_being_dropped(self):
        member = _Member(42, (next(iter(CORE)),))
        conversation = self.cog._conversations.create(GUILD_ID, 100, 90)
        self.cog._conversations.register_reply(conversation, 91)
        first = _message(author=member, bot_id=999, content="first")
        second = _message(author=member, bot_id=999, content="second")
        for message in (first, second):
            message.reference = SimpleNamespace(channel_id=100, message_id=91)
        started, release = asyncio.Event(), asyncio.Event()
        order = []
        async def answer(message, *args):
            order.append(message.content)
            if message is first:
                started.set()
                await release.wait()
        self.cog._answer.side_effect = answer
        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            task1 = asyncio.create_task(self.cog.on_message(first))
            await started.wait()
            task2 = asyncio.create_task(self.cog.on_message(second))
            await asyncio.sleep(0)
            self.assertEqual(order, ["first"])
            release.set()
            await asyncio.gather(task1, task2)
        self.assertEqual(order, ["first", "second"])
        self.assertFalse(self.cog._tasks)
        self.assertEqual(conversation.pending, 0)

    async def test_history_from_newly_inaccessible_sources_is_not_replayed(self):
        conversation = self.cog._conversations.create(GUILD_ID, 100, 90)
        conversation.turns = [ConversationTurn("permitted", frozenset({100})),
                              ConversationTurn("private evidence", frozenset({200}))]
        member = _Member(42, (next(iter(CORE)),))
        context = SimpleNamespace(
            state=AgentTurnState(), member=member,
            guild=SimpleNamespace(get_member=lambda member_id: member),
        )
        async def accessible(context, channel_id):
            return object() if channel_id == 100 else None
        with patch("elbow_helper.features.agent.conversation.preparation.accessible_message_channel", side_effect=accessible):
            history = await self.cog._conversation_history(conversation, context)
        self.assertEqual(history, "permitted")
        self.assertEqual(context.state.authorized_history, (conversation.turns[0],))
        self.assertEqual(context.state.history_status["included_turns"], 1)
        self.assertEqual(context.state.history_status["older_retained_turns_available"], 0)

    async def test_history_requiring_a_removed_role_is_not_replayed(self):
        core_only = _Member(42, (next(iter(CORE)),))
        conversation = self.cog._conversations.create(GUILD_ID, 100, 90)
        conversation.turns = [
            ConversationTurn("general", frozenset({100})),
            ConversationTurn(
                "restricted", frozenset({100}),
                required_access=frozenset({ACCESS_LEAD_PLUS}),
            ),
        ]
        context = SimpleNamespace(
            state=AgentTurnState(),
            guild=SimpleNamespace(get_member=lambda member_id: core_only),
            member=core_only,
        )
        with patch(
            "elbow_helper.features.agent.conversation.preparation.accessible_message_channel",
            return_value=object(),
        ):
            history = await self.cog._conversation_history(conversation, context)
        self.assertEqual(history, "general")
        self.assertEqual(
            context.state.authorized_history, (conversation.turns[0],),
        )

    async def test_checkpoint_requires_every_source_and_role_at_use_time(self):
        core_only = _Member(42, (next(iter(CORE)),))
        conversation = self.cog._conversations.create(GUILD_ID, 100, 90)
        conversation.turns = [
            ConversationTurn(
                f"turn {index}", frozenset({100, 200}),
                required_access=frozenset({ACCESS_LEAD_PLUS}),
            )
            for index in range(8)
        ]
        conversation.checkpoint = build_history_checkpoint(
            conversation.turns, covered_turn_count=8,
            created_at=datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
        )
        context = SimpleNamespace(
            state=AgentTurnState(),
            guild=SimpleNamespace(get_member=lambda member_id: core_only),
            member=core_only,
        )
        with patch(
            "elbow_helper.features.agent.conversation.preparation.accessible_message_channel",
            return_value=object(),
        ):
            await self.cog._conversation_history(conversation, context)
        self.assertIsNone(context.state.authorized_checkpoint)

        lead = _Member(42, (
            next(iter(CORE)), next(iter(LEAD_PLUS)),
        ))
        context.member = lead
        context.guild = SimpleNamespace(get_member=lambda member_id: lead)
        with patch(
            "elbow_helper.features.agent.conversation.preparation.accessible_message_channel",
            side_effect=lambda context, channel_id: (
                object() if channel_id == 100 else None
            ),
        ):
            await self.cog._conversation_history(conversation, context)
        self.assertIsNone(context.state.authorized_checkpoint)

        with patch(
            "elbow_helper.features.agent.conversation.preparation.accessible_message_channel",
            return_value=object(),
        ):
            await self.cog._conversation_history(conversation, context)
        self.assertIs(context.state.authorized_checkpoint, conversation.checkpoint)

    async def test_changed_knowledge_is_historical_even_after_report_eviction(self):
        reference = ("cwl_policy@v1", "a" * 64)
        conversation = self.cog._conversations.create(GUILD_ID, 100, 90)
        conversation.turns = [
            ConversationTurn(
                f"policy turn {index}", frozenset({100}),
                knowledge_refs=(reference,),
            )
            for index in range(8)
        ]
        conversation.checkpoint = build_history_checkpoint(
            conversation.turns, covered_turn_count=8,
            created_at=datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
        )
        self.cog.knowledge_store = SimpleNamespace(load=lambda: SimpleNamespace(
            references_are_current=lambda references: False,
        ))
        member = _Member(42, (next(iter(CORE)),))
        context = SimpleNamespace(
            state=AgentTurnState(), member=member,
            guild=SimpleNamespace(get_member=lambda member_id: member),
        )

        with patch(
            "elbow_helper.features.agent.conversation.preparation.accessible_message_channel",
            return_value=object(),
        ):
            history = await self.cog._conversation_history(conversation, context)

        self.assertIn("Historical context", history)
        self.assertEqual(context.state.stale_knowledge_refs, {reference})
        self.assertIsNone(context.state.authorized_checkpoint)

    async def test_current_knowledge_remains_usable_as_context(self):
        reference = ("cwl_policy@v2", "b" * 64)
        conversation = self.cog._conversations.create(GUILD_ID, 100, 90)
        conversation.turns = [ConversationTurn(
            "current policy turn", frozenset({100}),
            knowledge_refs=(reference,),
        )]
        self.cog.knowledge_store = SimpleNamespace(load=lambda: SimpleNamespace(
            references_are_current=lambda references: True,
        ))
        context = SimpleNamespace(state=AgentTurnState())
        with patch(
            "elbow_helper.features.agent.conversation.preparation.accessible_message_channel",
            return_value=object(),
        ):
            history = await self.cog._conversation_history(conversation, context)
        self.assertEqual(history, "current policy turn")
        self.assertEqual(context.state.stale_knowledge_refs, set())

    def test_checkpoint_refresh_requires_pressure_full_access_and_growth(self):
        conversation = self.cog._conversations.create(GUILD_ID, 100, 91)
        conversation.turns = [
            ConversationTurn(f"turn {index}", frozenset({100}))
            for index in range(9)
        ]
        state = AgentTurnState()
        state.authorized_history = tuple(conversation.turns[:8])
        state.history_status = {"older_retained_turns_available": 8}
        created_at = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)

        self.cog._refresh_history_checkpoint(
            conversation, state, created_at=created_at,
            previous_turn_count=8,
        )
        first = conversation.checkpoint
        self.assertIsNotNone(first)
        self.assertEqual(first.covered_turn_count, 8)

        self.cog._refresh_history_checkpoint(
            conversation, state, created_at=created_at,
            previous_turn_count=8,
        )
        self.assertIs(conversation.checkpoint, first)

        conversation.turns.extend(
            ConversationTurn(f"turn {index}", frozenset({100}))
            for index in range(9, 17)
        )
        state.authorized_history = tuple(conversation.turns[:16])
        state.history_status = {"older_retained_turns_available": 16}
        self.cog._refresh_history_checkpoint(
            conversation, state, created_at=created_at,
            previous_turn_count=16,
        )
        second = conversation.checkpoint
        self.assertEqual(second.covered_turn_count, 16)

        state.authorized_history = tuple(conversation.turns[:15])
        state.history_status = {"older_retained_turns_available": 24}
        self.cog._refresh_history_checkpoint(
            conversation, state, created_at=created_at,
            previous_turn_count=16,
        )
        self.assertIs(conversation.checkpoint, second)

    async def test_report_from_revoked_source_is_hidden_without_erasure(self):
        member = _Member(42, (next(iter(CORE)),))
        conversation = self.cog._conversations.create(GUILD_ID, 100, 90)
        report = RoleAccountReport("report", "2026-09-17", (), ())
        conversation.reports[report.report_id] = report
        conversation.report_sources[report.report_id] = frozenset({200})
        conversation.report_access_requirements[report.report_id] = frozenset()
        context = SimpleNamespace(
            state=AgentTurnState(source_channels={100}),
            guild=SimpleNamespace(get_member=lambda member_id: member),
            member=member,
        )
        with patch(
            "elbow_helper.features.agent.conversation.preparation.accessible_message_channel",
            return_value=None,
        ):
            await self.cog._load_authorized_reports(conversation, context)
        self.assertEqual(context.state.reports, {})
        self.assertIs(context.state.preserved_reports["report"], report)
        self.cog._commit_reports(conversation, context.state)
        self.assertIs(conversation.reports["report"], report)

    async def test_stale_knowledge_report_is_hidden_and_preserved(self):
        class _KnowledgeReport:
            report_id = "knowledge"
            sections = ()

        member = _Member(42, (next(iter(CORE)),))
        conversation = self.cog._conversations.create(GUILD_ID, 100, 90)
        report = _KnowledgeReport()
        conversation.reports[report.report_id] = report
        conversation.report_sources[report.report_id] = frozenset({100})
        conversation.report_access_requirements[report.report_id] = frozenset()
        self.cog.knowledge_store = SimpleNamespace(load=lambda: SimpleNamespace(
            sections_are_current=lambda sections: False,
        ))
        context = SimpleNamespace(
            state=AgentTurnState(source_channels={100}),
            guild=SimpleNamespace(get_member=lambda member_id: member),
            member=member,
        )
        with patch(
            "elbow_helper.features.agent.conversation.preparation.KnowledgeReport",
            _KnowledgeReport,
        ):
            await self.cog._load_authorized_reports(conversation, context)
        self.assertEqual(context.state.reports, {})
        self.assertIs(context.state.preserved_reports["knowledge"], report)
        self.assertEqual(
            context.state.stale_knowledge_report_ids, {"knowledge"},
        )

    async def test_history_preparation_preserves_all_authorized_candidates_for_compilation(self):
        conversation = self.cog._conversations.create(GUILD_ID, 100, 90)
        conversation.turns = [ConversationTurn("older " + "x" * 60_000, frozenset({100})),
                              ConversationTurn("latest", frozenset({100}))]
        context = SimpleNamespace(state=AgentTurnState())
        with patch("elbow_helper.features.agent.conversation.preparation.accessible_message_channel", return_value=object()):
            history = await self.cog._conversation_history(conversation, context)
        self.assertIn("older", history)
        self.assertEqual(context.state.authorized_history, tuple(conversation.turns))
        self.assertEqual(len(conversation.turns), 2)

    async def test_inaccessible_task_instruction_is_not_prepared_for_model(self):
        conversation = self.cog._conversations.create(GUILD_ID, 100, 90)
        working, instruction = WorkingState().remember(
            label="Constraint", quote="Keep together", request_text="Keep together",
            member_id=42, message_id=1, channel_id=200, created_at="2026-09-17",
        )
        context = SimpleNamespace(state=AgentTurnState(working=working))
        with patch("elbow_helper.features.agent.conversation.preparation.accessible_message_channel", return_value=None):
            await self.cog._conversation_history(conversation, context)
        self.assertEqual(context.state.authorized_instructions, ())
        self.assertEqual(context.state.working.instructions, (instruction,))

    async def test_task_instruction_survives_delivered_turn_and_reaches_followup(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        async def answer(**kwargs):
            state = kwargs["context"].state
            state.working, _ = state.working.remember(
                label="Constraint", quote="Keep together", request_text="Keep together",
                member_id=42, message_id=1, channel_id=100, created_at="2026-09-17",
            )
            return "Understood."
        self.cog.service.answer.side_effect = answer
        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            await self.cog.on_message(make_message(member, 1, "Keep together"))
            self.cog.service.answer.side_effect = None
            self.cog.service.answer.return_value = "Draft ready."
            await self.cog.on_message(make_message(member, 2, "Continue", reply_to=1001))
        context = self.cog.service.answer.await_args.kwargs["context"]
        self.assertEqual(context.state.authorized_instructions[0].quote, "Keep together")

    async def test_timeout_includes_waiting_for_capacity(self):
        self.cog._semaphore = asyncio.Semaphore(0)
        self.cog._send_failure = AsyncMock()
        member = _Member(42, (next(iter(CORE)),))
        message = _message(author=member, bot_id=999, content="<@999> hello")
        with (
            patch("elbow_helper.features.agent.cog.discord.Member", _Member),
            patch("elbow_helper.features.agent.cog.AGENT_REQUEST_TIMEOUT_SECONDS", 0.01),
        ):
            await self.cog.on_message(message)
        self.cog._answer.assert_not_awaited()
        self.cog._send_failure.assert_awaited_once()
        self.assertFalse(self.cog._tasks)

    def test_embed_content_is_available_as_evidence(self):
        embed = discord.Embed(title="Applicant Review", description="Discussion")
        embed.add_field(name="Player", value="Example")
        text = message_text(SimpleNamespace(content="", embeds=[embed], attachments=[]))
        self.assertIn("Applicant Review", text)
        self.assertIn("Player: Example", text)

    async def test_local_context_uses_nearest_messages_in_chronological_order(self):
        items = [SimpleNamespace(
            id=i, content=f"text-{i}", attachments=[], embeds=[],
            author=SimpleNamespace(id=42, bot=False, display_name="Member"),
            created_at=datetime.now(timezone.utc), jump_url=f"source/{i}",
        ) for i in range(20)]
        async def history(*, limit, before, oldest_first):
            selected = items[:limit] if oldest_first else list(reversed(items))[:limit]
            for item in selected:
                yield item
        message = SimpleNamespace(channel=SimpleNamespace(id=100, history=history), mentions=[])
        result = await self.cog._build_local_context(message, None)
        self.assertIn("text-12", result)
        self.assertIn("text-19", result)
        self.assertNotIn("text-0", result)
        self.assertLess(result.index("text-12"), result.index("text-19"))

    async def test_long_reply_preserves_complete_answer_as_attachment(self):
        message = SimpleNamespace(id=1, mentions=[], reply=AsyncMock())
        response = "answer " * 3000
        await self.cog._send_response(message, response, None)
        attached = message.reply.await_args.kwargs["files"][0]
        self.assertEqual(attached.fp.read().decode("utf-8"), response)
        attached.close()

    def setUp(self) -> None:
        self.bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            agent_model=MagicMock(),
        )
        self.cog = CoreAgent(
            self.bot,
            account_links=object(),
            clan_health=object(),
            message_search=object(),
            roster_queries=object(),
            cwl_queries=object(),
        )
        self.cog._answer = AsyncMock()

    async def test_core_mention_starts_agent_with_clean_question(self) -> None:
        member = _Member(42, (next(iter(CORE)),))
        message = _message(
            author=member,
            bot_id=999,
            content="<@999> tell this guy to piss off",
        )

        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            await self.cog.on_message(message)

        self.cog._answer.assert_awaited_once_with(
            message,
            member,
            "tell this guy to piss off",
            ANY,
        )

    async def test_non_core_mention_is_silently_ignored(self) -> None:
        member = _Member(42, ())
        message = _message(
            author=member,
            bot_id=999,
            content="<@999> hello",
        )

        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            await self.cog.on_message(message)

        self.cog._answer.assert_not_awaited()

    def test_message_without_direct_bot_mention_is_ignored(self) -> None:
        member = _Member(42, (next(iter(CORE)),))
        message = SimpleNamespace(
            author=member,
            guild=SimpleNamespace(id=GUILD_ID),
            raw_mentions=[],
            content="talking about the bot",
        )

        self.assertFalse(self.cog._is_agent_request(message))


if __name__ == "__main__":
    unittest.main()
