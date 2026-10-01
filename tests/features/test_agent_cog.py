from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
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
from elbow_helper.discord.interactions import DEFAULT_FAILURE_MESSAGE
from elbow_helper.features.agent.access import ACCESS_LEAD_PLUS, AgentAccessLost
from elbow_helper.features.agent.cog import AgentCog
from elbow_helper.features.agent.delivery import _delivery_nonce, _needs_file
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
from elbow_helper.features.agent.service import AgentService
from elbow_helper.infrastructure.ai.client import DeepSeekTextClient
from elbow_helper.infrastructure.ai import AgentToolDefinition
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.features.agent.capabilities import CapabilityContract


class _Member:
    def __init__(self, member_id: int, role_ids: tuple[int, ...]):
        self.id = member_id
        self.bot = False
        self.display_name = f"Member {member_id}"
        self.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]


def _message(*, author: _Member, bot_id: int, content: str):
    channel = SimpleNamespace(
        id=100,
        type=discord.ChannelType.text,
        overwrites={},
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


class AgentCogTests(unittest.IsolatedAsyncioTestCase):
    def test_discord_registers_only_the_message_handler(self):
        self.assertIn(("on_message", "on_message"), AgentCog.__cog_listeners__)
        self.assertNotIn(("_run_member_request", "_run_member_request"),
                         AgentCog.__cog_listeners__)

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

    async def test_checked_source_reads_reach_discord_through_real_orchestration(self):
        for extra_final_call in (False, True):
            with self.subTest(extra_final_call=extra_final_call):
                self.setUp()
                member = _Member(42, (next(iter(CORE)),))
                make_message = self._real_handler_scenario(member)
                message = make_message(member, 1000, "<@999> read #source-one and #source-two")
                channels = {100: message.channel}
                for channel_id, name in ((200, "source-one"), (300, "source-two")):
                    channels[channel_id] = SimpleNamespace(
                        id=channel_id, name=name, guild=message.guild,
                        type=discord.ChannelType.text, overwrites={},
                        permissions_for=message.channel.permissions_for,
                    )
                message.channel.name = "request-room"
                message.guild.channels = list(channels.values())
                message.guild.threads = []
                message.guild.get_channel_or_thread = channels.get
                calls = []

                async def read_value(context, arguments):
                    source_id = arguments["channel_id"]
                    calls.append(source_id)
                    context.state.source_channels.add(source_id)
                    return {"channel_id": source_id, "value": source_id}

                registry = {"read_value": RegisteredAgentTool(
                    AgentToolDefinition("read_value", "Read a selected value.", {
                        "type": "object", "properties": {
                            "channel_id": {"type": "integer", "minimum": 1},
                        }, "required": ["channel_id"],
                    }), read_value,
                )}
                contract = CapabilityContract(
                    (("channel_id", "discord_channel"),), (),
                    source_scope="channel_messages", channel_fields=("channel_id",),
                    result_channel_fields=("channel_id",),
                )
                plan = {
                    "goal": "Read selected values", "effort": "low", "output": "text",
                    "periods": [], "entities": [
                        {"kind": "discord_channel", "value": channel_id}
                        for channel_id in (200, 300)
                    ],
                    "steps": [
                        {"id": str(channel_id), "capability": "read_value",
                         "arguments": {"channel_id": channel_id},
                         "reason": "Read selected source", "depends_on": []}
                        for channel_id in (200, 300)
                    ],
                }

                def response(*, content=None, tool_calls=None):
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message={
                            "role": "assistant", "content": content,
                            "tool_calls": tool_calls, "reasoning_content": "retained reasoning",
                        })],
                        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100),
                    )

                planned = response(tool_calls=[{
                    "id": "plan", "function": {
                        "name": "submit_request_plan", "arguments": json.dumps(plan),
                    },
                }])
                answer = "Both selected values are ready."
                responses = [planned]
                if extra_final_call:
                    responses.append(response(tool_calls=[{
                        "id": "extra", "function": {
                            "name": "submit_request_plan", "arguments": json.dumps(plan),
                        },
                    }]))
                responses.append(response(content=answer))
                create = AsyncMock(side_effect=responses)
                transport = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                with (
                    patch("elbow_helper.infrastructure.ai.client.AsyncOpenAI", return_value=transport),
                    patch("elbow_helper.features.agent.cog.discord.Member", _Member),
                    patch("elbow_helper.features.agent.service.build_agent_tools", return_value=registry),
                    patch.dict("elbow_helper.features.agent.capabilities.CONTRACTS", {"read_value": contract}),
                    patch("elbow_helper.features.agent.service.MAX_MODEL_ROUNDS", 2),
                ):
                    self.cog.service = AgentService(DeepSeekTextClient("test-key"))
                    await self.cog.on_message(message)

                message.reply.assert_awaited_once()
                self.assertEqual(message.reply.await_args.args, (answer,))
                self.assertEqual(sorted(calls), [200, 300])
                self.assertEqual(create.await_count, 3 if extra_final_call else 2)
                self.assertEqual(create.await_args_list[0].kwargs["reasoning_effort"], "low")
                self.assertEqual([
                    item["function"]["name"]
                    for item in create.await_args_list[0].kwargs["tools"]
                ], ["submit_request_plan"])
                answer_request = create.await_args_list[1].kwargs
                self.assertEqual(answer_request["tool_choice"], "none")
                result = next(item["content"] for item in answer_request["messages"]
                              if item.get("role") == "tool")
                self.assertIn('"200"', result)
                self.assertIn('"300"', result)
                conversation = self.cog._conversations.find(GUILD_ID, 100, 2000)
                self.assertTrue(conversation.turns[0].record.delivery_complete)
                self.assertEqual(conversation.turns[0].source_channels,
                                 frozenset({100, 200, 300}))

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

    async def test_restricted_report_survives_temporary_role_loss_in_private_channel(self):
        core_role = next(iter(CORE))
        lead_role = next(iter(LEAD_PLUS))
        lead = _Member(42, (core_role, lead_role))
        make_message = self._real_handler_scenario(lead)
        first = make_message(lead, 1, "<@999> restricted")
        channel = first.channel
        guild = first.guild
        default_role = SimpleNamespace(id=1)
        allowed_role = type("Role", (), {"id": lead_role})()
        guild.default_role = default_role
        guild.roles = [default_role, SimpleNamespace(id=core_role), allowed_role]
        channel.overwrites = {allowed_role: SimpleNamespace(
            view_channel=True, read_message_history=True,
        )}

        def permissions_for(actor):
            visible = actor.id == 999 or any(
                role.id == lead_role for role in getattr(actor, "roles", ())
            )
            return SimpleNamespace(view_channel=visible, read_message_history=visible)

        channel.permissions_for = permissions_for
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
            await self.cog.on_message(first)
            lead.roles = [role for role in lead.roles if role.id != lead_role]
            await self.cog.on_message(make_message(lead, 2, "continue", reply_to=1001))
            lead.roles.append(SimpleNamespace(id=lead_role))
            await self.cog.on_message(make_message(lead, 3, "continue", reply_to=1001))

        self.assertEqual(observed, [(), ("restricted",)])
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
            self.cog = AgentCog(self.bot, account_links=object(), clan_health=object(),
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
            type=discord.ChannelType.text,
            overwrites={},
            typing=lambda: nullcontext(),
            permissions_for=lambda actor: SimpleNamespace(
                view_channel=True, read_message_history=True,
            ),
            send=AsyncMock(return_value=SimpleNamespace(id=9000)),
        )
        by_id = {member.id: member for member in members}
        default_role = SimpleNamespace(id=1)
        guild = SimpleNamespace(
            id=GUILD_ID,
            name="Brown Elbow",
            me=SimpleNamespace(id=999),
            default_role=default_role,
            roles=[default_role, *(
                SimpleNamespace(id=role_id)
                for role_id in sorted({
                    role.id for member in members for role in member.roles
                })
            )],
            get_member=by_id.get,
            get_channel_or_thread=lambda value: channel if value == channel.id else None,
        )
        channel.guild = guild
        self.cog._answer = AgentCog._answer.__get__(self.cog, AgentCog)
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

        async def wait_for_cancellation(*args, **kwargs):
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
        self.cog._answer = AgentCog._answer.__get__(self.cog, AgentCog)
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
        self.cog._answer.assert_awaited_once_with(
            message, member, message.content, conversation,
            deadline_monotonic=ANY,
        )
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
        async def answer(message, *args, **kwargs):
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

    @patch("elbow_helper.features.agent.conversation.preparation.can_disclose_provenance", new=AsyncMock(return_value=True))
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

    @patch("elbow_helper.features.agent.conversation.preparation.can_disclose_provenance", new=AsyncMock(return_value=True))
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

    @patch("elbow_helper.features.agent.conversation.preparation.can_disclose_provenance", new=AsyncMock(return_value=True))
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

    @patch("elbow_helper.features.agent.conversation.preparation.can_disclose_provenance", new=AsyncMock(return_value=True))
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

    @patch("elbow_helper.features.agent.conversation.preparation.can_disclose_provenance", new=AsyncMock(return_value=True))
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

    @patch("elbow_helper.features.agent.conversation.preparation.can_disclose_provenance", new=AsyncMock(return_value=True))
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

    async def test_action_result_keeps_instruction_saved_after_preview(self):
        latest, _ = WorkingState().remember(
            label="Constraint", quote="Keep together", request_text="Keep together",
            member_id=42, message_id=2, channel_id=100, created_at="2026-09-17",
        )
        conversation = SimpleNamespace(
            lock=asyncio.Lock(), working=latest, append=MagicMock(),
        )
        self.cog._conversations = SimpleNamespace(get=lambda _: conversation)
        context = SimpleNamespace(
            conversation_root_id=1, state=AgentTurnState(working=WorkingState()),
        )
        await self.cog._record_action_outcome(
            context, {"run_id": "synthetic", "status": "completed", "steps": []}, None,
        )
        self.assertIs(conversation.working, latest)
        conversation.append.assert_called_once()

    async def test_queue_wait_has_its_own_timeout(self):
        self.cog._semaphore = asyncio.Semaphore(0)
        self.cog._send_failure = AsyncMock()
        member = _Member(42, (next(iter(CORE)),))
        message = _message(author=member, bot_id=999, content="<@999> hello")
        with (
            patch("elbow_helper.features.agent.cog.discord.Member", _Member),
            patch("elbow_helper.features.agent.cog.AGENT_QUEUE_WAIT_TIMEOUT_SECONDS", 0.01),
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

    async def test_local_context_anchors_old_reply_at_referenced_message(self):
        anchors = []
        old = SimpleNamespace(
            id=10, content="original point", attachments=[], embeds=[],
            author=SimpleNamespace(id=41, bot=False, display_name="Member"),
            created_at=datetime.now(timezone.utc), jump_url="source/10",
        )
        nearby = SimpleNamespace(
            id=9, content="relevant context", attachments=[], embeds=[],
            author=old.author, created_at=old.created_at, jump_url="source/9",
        )
        async def history(*, limit, before, oldest_first):
            anchors.append(before)
            yield nearby
        message = SimpleNamespace(
            id=100, channel=SimpleNamespace(id=100, history=history), mentions=[],
        )

        result = await self.cog._build_local_context(message, old)

        self.assertEqual(anchors, [old])
        self.assertIn("relevant context", result)
        self.assertIn("original point", result)

    async def test_nearby_agent_messages_from_other_conversations_show_their_age(self):
        current = self.cog._conversations.create(GUILD_ID, 100, 10)
        other = self.cog._conversations.create(GUILD_ID, 100, 20)
        self.cog._conversations.register_reply(current, 11)
        self.cog._conversations.register_reply(other, 21)
        now = datetime.now(timezone.utc)
        author = SimpleNamespace(id=42, bot=False, display_name="Member")
        bot = SimpleNamespace(id=999, bot=True, display_name="Elbow Helper")

        def nearby(message_id, sender, *, mentions=()):
            return SimpleNamespace(
                id=message_id, content=f"text-{message_id}", attachments=[], embeds=[],
                author=sender, raw_mentions=mentions,
                created_at=now - timedelta(seconds=60), jump_url=f"source/{message_id}",
            )

        items = [nearby(11, bot), nearby(20, author, mentions=(999,)),
                 nearby(21, bot), nearby(22, bot)]

        async def history(*, limit, before, oldest_first):
            for item in reversed(items):
                yield item

        message = SimpleNamespace(
            id=30, guild=SimpleNamespace(id=GUILD_ID), created_at=now,
            channel=SimpleNamespace(id=100, history=history), mentions=[],
        )
        result = await self.cog._build_local_context(message, None, current)

        self.assertIn("message_id=11", result)
        own_line = next(line for line in result.splitlines() if "message_id=11" in line)
        self.assertNotIn("other agent conversation", own_line)
        for message_id in (20, 21, 22):
            line = next(line for line in result.splitlines() if f"message_id={message_id}" in line)
            self.assertIn("other agent conversation, 60s old", line)

    async def test_long_reply_preserves_complete_answer_as_attachment(self):
        message = SimpleNamespace(id=1, mentions=[], reply=AsyncMock())
        response = "answer " * 3000
        await self.cog._send_response(message, response, None)
        attached = message.reply.await_args.kwargs["files"][0]
        self.assertEqual(attached.fp.read().decode("utf-8"), response)
        attached.close()

    async def test_short_code_block_stays_in_channel(self):
        message = SimpleNamespace(id=1, mentions=[], reply=AsyncMock())
        response = "```text\nhello\n```"
        await self.cog._send_response(message, response, None)
        self.assertEqual(message.reply.await_args.args[0], response)
        self.assertEqual(message.reply.await_args.kwargs.get("files"), None)

    async def test_file_only_when_length_or_split_code_block_requires_it(self):
        self.assertFalse(_needs_file("```text\nshort\n```"))
        self.assertFalse(_needs_file("plain " * 400))
        self.assertTrue(_needs_file("```text\n" + "x" * 2100 + "\n```"))
        self.assertTrue(_needs_file("x" * 12_001))

    def setUp(self) -> None:
        self.bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            agent_model=MagicMock(),
        )
        self.cog = AgentCog(
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
            deadline_monotonic=ANY,
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
