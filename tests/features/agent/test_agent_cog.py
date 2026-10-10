from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timezone
from contextlib import asynccontextmanager, nullcontext
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
from elbow_helper.features.agent.text import message_text
from elbow_helper.features.agent.conversation.state import ConversationTurn
from elbow_helper.features.agent.models import AgentDelivery, AgentTurnState
from elbow_helper.features.agent.conversation.context import compile_context
from features.agent.report_helpers import make_event_report
from elbow_helper.features.agent.conversation.instructions import WorkingState
from elbow_helper.features.agent.conversation.transcripts import TranscriptArchive
from elbow_helper.features.agent.conversation.repository import ConversationRepository
from elbow_helper.features.agent.conversation.codec import decode_conversation
from elbow_helper.features.agent.conversation.persistence import ConversationPersistence
from elbow_helper.features.agent.conversation.context import build_history_checkpoint
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.infrastructure.ai.client import DeepSeekTextClient
from elbow_helper.infrastructure.ai import AgentToolDefinition
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.features.agent.engine.capability_contract import CapabilityContract

from features.agent.engine.helpers import patch_contracts


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
    async def test_direct_answer_is_delivered_without_typing(self):
        from features.agent.engine.test_agent_plan_flow import _Model, _Session
        from elbow_helper.infrastructure.ai import AgentStep, AgentUsage

        member = _Member(121, (next(iter(CORE)),))
        message = self._real_handler_scenario(member)(member, 501, "<@999> synthetic question")
        message.channel.typing = MagicMock(return_value=nullcontext())
        session = _Session([AgentStep("Synthetic answer", (), AgentUsage())], [])
        self.cog.service = AgentService(_Model(session))
        with (
            patch("elbow_helper.features.agent.cog.discord.Member", _Member),
            patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value={}),
        ):
            await self.cog.on_message(message)
        message.channel.typing.assert_not_called()
        message.reply.assert_awaited_once()
        self.assertEqual(message.reply.await_args.args[0], "Synthetic answer")
        self.assertEqual(len(session.calls), 1)

    async def test_lookup_plan_starts_typing_after_first_round_and_keeps_it_through_delivery(self):
        from features.agent.engine.test_agent_plan_flow import (
            _Model, _Session, _model_step, _plan, _step,
        )
        from elbow_helper.infrastructure.ai import AgentStep, AgentUsage

        events = []

        @asynccontextmanager
        async def typing():
            events.append("typing")
            try:
                yield
            finally:
                events.append("typing ended")

        async def read(context, arguments):
            events.append("lookup")
            return {"value": 13}

        member = _Member(121, (next(iter(CORE)),))
        message = self._real_handler_scenario(member)(member, 501, "<@999> synthetic question")
        message.channel.typing = MagicMock(side_effect=typing)
        sent = message.reply.return_value

        async def deliver(*args, **kwargs):
            events.append("delivery")
            return sent

        message.reply.side_effect = deliver
        registry = {"read_value": RegisteredAgentTool(
            AgentToolDefinition("read_value", "Read synthetic data.", {
                "type": "object", "properties": {}, "required": [],
            }), read, contract=CapabilityContract(()),
        )}
        session = _Session([
            _model_step(_plan([_step("lookup")])),
            AgentStep("Synthetic answer", (), AgentUsage()),
        ], events)
        self.cog.service = AgentService(_Model(session))
        with (
            patch("elbow_helper.features.agent.cog.discord.Member", _Member),
            patch("elbow_helper.features.agent.engine.service.build_agent_tools",
                  return_value=registry),
        ):
            await self.cog.on_message(message)
        self.assertEqual(events, [
            "model", "typing", "lookup", "model", "delivery", "typing ended",
        ])
        message.channel.typing.assert_called_once()

    async def test_reaction_plans_deliver_and_record_only_the_reply_needed(self):
        from features.agent.engine.test_agent_plan_flow import (
            _Model, _Session, _model_step, _plan, _step,
        )
        from elbow_helper.features.agent.discord_actions.reactions import reaction_tools
        from elbow_helper.infrastructure.ai import AgentStep, AgentUsage

        for mode in ("silent", "text", "failed", "partial", "mixed"):
            with self.subTest(mode=mode):
                self.setUp()
                member = _Member(121, (next(iter(CORE)),))
                make_message = self._real_handler_scenario(member)
                message = make_message(member, 501, "<@999> thanks")
                message.add_reaction = AsyncMock()
                reaction = reaction_tools()[0]
                registry = {reaction.definition.name: reaction}
                steps = [{**_step("reaction", {"emoji": "👍"}),
                          "capability": "react_to_request"}]
                if mode == "failed":
                    message.add_reaction.side_effect = discord.HTTPException(
                        SimpleNamespace(status=403, reason="Forbidden"), "Unavailable",
                    )
                if mode == "partial":
                    steps.append({**_step("second", {"emoji": "👏"}, ["reaction"]),
                                  "capability": "react_to_request"})
                    message.add_reaction.side_effect = [None, discord.HTTPException(
                        SimpleNamespace(status=403, reason="Forbidden"), "Unavailable",
                    )]
                if mode == "mixed":
                    registry["read_value"] = RegisteredAgentTool(
                        AgentToolDefinition(
                            "read_value", "Read a synthetic value.",
                            {"type": "object", "properties": {}, "required": []},
                        ),
                        AsyncMock(return_value={"value": 7}),
                        contract=CapabilityContract(()),
                    )
                    steps.append(_step("read"))
                first = _model_step(_plan(steps))
                if mode == "text":
                    first = AgentStep("You're welcome.", first.tool_calls, AgentUsage())
                rounds = [first]
                fallback = mode in ("failed", "partial", "mixed")
                if fallback:
                    rounds.append(AgentStep("Normal answer.", (), AgentUsage()))
                session = _Session(rounds, [])
                message.channel.typing = MagicMock(return_value=nullcontext())
                self.cog.service = AgentService(_Model(session))
                with (
                    patch("elbow_helper.features.agent.cog.discord.Member", _Member),
                    patch("elbow_helper.features.agent.engine.service.build_agent_tools",
                          return_value=registry),
                ):
                    await self.cog.on_message(message)
                if mode == "mixed":
                    message.channel.typing.assert_called_once()
                else:
                    message.channel.typing.assert_not_called()
                self.assertEqual(len(session.calls), 2 if fallback else 1)
                self.assertEqual(message.add_reaction.await_args_list[0].args, ("👍",))
                self.assertEqual(message.add_reaction.await_count, 2 if mode == "partial" else 1)
                message.channel.send.assert_not_awaited()
                conversation = self.cog._conversations.get(message.id)
                self.assertEqual(len(conversation.turns), 1)
                answer = json.loads(conversation.turns[0].text)["answer"]
                if mode == "silent":
                    message.reply.assert_not_awaited()
                    self.assertEqual(answer, "Reactions: 👍")
                    self.assertTrue(conversation.turns[0].record.delivery_complete)
                    self.assertEqual(conversation.turns[0].record.reply_ids, ())
                else:
                    message.reply.assert_awaited_once()
                    expected = "You're welcome." if mode == "text" else "Normal answer."
                    self.assertIn(expected, message.reply.await_args.args[0])
                    self.assertIn(expected, answer)
                    self.assertEqual("Reactions: 👍" in answer, mode != "failed")


    async def test_mixed_answer_and_preview_share_delivery_and_records(self):
        from elbow_helper.features.agent.actions.contracts import PreparedAction, ChangePreview
        from elbow_helper.features.agent.actions.preview import ConfirmationView, preview_text
        from elbow_helper.features.agent.actions.answer_view import PrivateAnswerView
        from elbow_helper.features.agent.actions.private_view import PrivateResultView
        from elbow_helper.features.agent.actions.combined_reply import CombinedReplyView
        from elbow_helper.features.agent.actions.outcomes import ActionOutcome
        from elbow_helper.features.agent.wording import ACTION_PRIVATE_ANSWER

        for allowed in (True, False):
            with self.subTest(allowed=allowed):
                self.setUp()
                member = _Member(121, (next(iter(CORE)),))
                make_message = self._real_handler_scenario(member)
                message = make_message(member, 501, "<@999> change synthetic target and list synthetic values")
                proposal = PreparedAction(
                    "/synthetic", {"target": 321},
                    ChangePreview(("Change synthetic target",), AsyncMock(return_value=True)), AsyncMock(),
                )
                preview = preview_text((proposal,))
                answer_message = SimpleNamespace(id=1501, edit=AsyncMock())
                answer_message.edit.return_value = answer_message
                message.reply.return_value = answer_message
                self.cog.action_runner = SimpleNamespace(submit=AsyncMock(return_value="synthetic-run"))
                self.cog._archive_reply = AsyncMock()

                async def answer(**kwargs):
                    state = kwargs["context"].state
                    state.proposed_changes.append(proposal)
                    state.preview_reply = preview
                    state.preview_first = True
                    state.outcomes.append(ActionOutcome(
                        "complete", "private", private_parts=("Synthetic command result",),
                    ))
                    return "Synthetic lookup answer"

                self.cog.service.answer.side_effect = answer
                with (patch("elbow_helper.features.agent.cog.discord.Member", _Member),
                      patch("elbow_helper.features.agent.delivery.can_show", AsyncMock(return_value=allowed))):
                    await self.cog.on_message(message)
                calls = message.reply.await_args_list
                self.assertEqual(len(calls), 1)
                combined = calls[0].kwargs["view"]
                self.addCleanup(combined.stop)
                self.assertIsInstance(combined, CombinedReplyView)
                answer_part = "Synthetic lookup answer" if allowed else ACTION_PRIVATE_ANSWER
                self.assertEqual(calls[0].args[0], f"1. {preview}\n\n2. {answer_part}")
                answer_view = combined.views["answer"]
                self.assertIsInstance(answer_view, PrivateResultView if allowed else PrivateAnswerView)
                self.assertIn("Synthetic command result", answer_view.parts)
                confirm = combined.views["preview"]
                self.assertIsInstance(confirm, ConfirmationView)
                self.assertIsNone(confirm.private_result)
                self.cog._archive_reply.assert_any_await(message.id, answer_message.id, combined.render())
                conversation = self.cog._conversations.find(GUILD_ID, 100, answer_message.id)
                record = conversation.turns[0].record
                self.assertEqual(record.reply_ids, (1501,))
                self.assertIn(preview, record.delivered_answer)
                self.assertIn(calls[0].args[0], record.delivered_answer)
                interaction = SimpleNamespace(user=member, response=SimpleNamespace(defer=AsyncMock()))
                await confirm.confirm(interaction)
                self.cog.action_runner.submit.assert_awaited_once()
                progress = self.cog.action_runner.submit.await_args.kwargs["progress_message"]
                self.assertTrue(progress.preserves_other_text)
                self.assertEqual(progress.id, 1501)
                answer_message.edit.assert_not_awaited()
                if not allowed:
                    interaction.id = 601
                    await answer_view.post_here(interaction)
                    record = conversation.turns[0].record
                    self.assertEqual(record.reply_ids, (1501,))
                    self.assertIn("Synthetic lookup answer", record.delivered_answer)
                    self.assertIn(preview, record.delivered_answer)
                    self.assertEqual(record.generated_parts, (combined.render(),))
                    self.assertIsInstance(combined.views["answer"], PrivateResultView)

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
                    (("channel_id", "discord_channel"),), source_scope="channel_messages",
                    channel_fields=("channel_id",), result_channel_fields=("channel_id",),
                )
                plan = {
                    "goal": "Read selected values", "effort": "low", "output": "text",
                    "steps": [{
                        "id": str(channel_id), "capability": "read_value",
                        "arguments": {"channel_id": channel_id},
                        "reason": "Read selected source", "depends_on": [],
                    } for channel_id in (200, 300)],
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
                    patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry),
                    patch_contracts(registry, {"read_value": contract}),
                    patch("elbow_helper.features.agent.engine.budgets.MAX_MODEL_ROUNDS", 2),
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
        report = make_event_report("restricted", "2026-09-17")
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
            self.cog = AgentCog(self.bot, account_links=object(),
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

        await self.cog.send_response(message, "x" * 2100, None, conversation=conversation)

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
        report = make_event_report(report_id="report", created_at="2026-09-16")

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
        report = make_event_report("report", "2026-09-17")
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

    async def test_local_context_excludes_other_agent_conversations(self):
        current = self.cog._conversations.create(GUILD_ID, 100, 10)
        other = self.cog._conversations.create(GUILD_ID, 100, 20)
        self.cog._conversations.register_reply(current, 11)
        self.cog._conversations.register_reply(other, 21)
        now = datetime.now(timezone.utc)
        author = SimpleNamespace(id=42, bot=False, display_name="Member")
        bot = SimpleNamespace(id=999, bot=True, display_name="Elbow Helper")

        def nearby(message_id, sender, *, mentions=(), reply_to=None, resolved=None):
            return SimpleNamespace(
                id=message_id, content=f"text-{message_id}", attachments=[], embeds=[],
                author=sender, raw_mentions=mentions,
                reference=(SimpleNamespace(message_id=reply_to, channel_id=100, resolved=resolved)
                           if reply_to is not None else None),
                created_at=now, jump_url=f"source/{message_id}",
            )

        items = [nearby(10, author, mentions=(999,)), nearby(11, bot),
                 nearby(20, author, mentions=(999,)), nearby(21, bot), nearby(22, bot),
                 nearby(23, author, mentions=(999,)), nearby(24, author, reply_to=21),
                 nearby(25, author), nearby(26, author, reply_to=11),
                 nearby(27, author, reply_to=90, resolved=SimpleNamespace(author=bot))]

        async def history(*, limit, before, oldest_first):
            for item in reversed(items):
                yield item

        message = SimpleNamespace(
            id=30, guild=SimpleNamespace(id=GUILD_ID), created_at=now,
            channel=SimpleNamespace(id=100, history=history), mentions=[],
        )
        result = await self.cog._build_local_context(message, None, current)

        for message_id in (10, 11, 25, 26):
            self.assertIn(f"text-{message_id}", result)
        for message_id in (20, 21, 22, 23, 24, 27):
            self.assertNotIn(f"text-{message_id}", result)
        self.assertLess(result.index("text-11"), result.index("text-25"))

        excluded = [*items[2:7], items[-1]]
        for referenced in excluded:
            with self.subTest(referenced=referenced.id):
                result = await self.cog._build_local_context(message, referenced, current)
                self.assertIn("Message directly replied to by the asker:", result)
                self.assertEqual(result.count(f"text-{referenced.id}"), 1)
                for item in excluded:
                    if item is not referenced:
                        self.assertNotIn(f"text-{item.id}", result)

        result = await self.cog._build_local_context(message, None)
        for message_id in (10, 11, 20, 21, 22, 23, 24, 26, 27):
            self.assertNotIn(f"text-{message_id}", result)
        self.assertIn("text-25", result)

    async def test_long_reply_preserves_complete_answer_as_attachment(self):
        message = SimpleNamespace(id=1, mentions=[], reply=AsyncMock())
        response = "answer " * 3000
        await self.cog.send_response(message, response, None)
        attached = message.reply.await_args.kwargs["files"][0]
        self.assertEqual(attached.fp.read().decode("utf-8"), response)
        attached.close()

    async def test_short_code_block_stays_in_channel(self):
        message = SimpleNamespace(id=1, mentions=[], reply=AsyncMock())
        response = "```text\nhello\n```"
        await self.cog.send_response(message, response, None)
        self.assertEqual(message.reply.await_args.args[0], response)
        self.assertEqual(message.reply.await_args.kwargs.get("files"), None)

    async def test_file_only_when_length_or_split_code_block_requires_it(self):
        self.assertFalse(_needs_file("```text\nshort\n```"))
        self.assertFalse(_needs_file("plain " * 400))
        self.assertTrue(_needs_file("```text\n" + "x" * 2100 + "\n```"))
        self.assertTrue(_needs_file("x" * 12_001))

    def setUp(self) -> None:
        self.bot = SimpleNamespace(
            user=SimpleNamespace(id=999, display_name="Synthetic Agent"),
            agent_model=MagicMock(),
        )
        self.cog = AgentCog(
            self.bot,
            account_links=object(),
            message_search=object(),
            roster_queries=object(),
            cwl_queries=object(),
        )
        self.cog._answer = AsyncMock()

    async def test_core_mention_preserves_agent_name_in_question(self) -> None:
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
            "@Synthetic Agent tell this guy to piss off",
            ANY,
            deadline_monotonic=ANY,
        )

    async def test_bot_mentions_at_start_and_mid_sentence_reach_the_model(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            for index, (original, expected) in enumerate((
                ("<@999> help Synthetic Member", "@Synthetic Agent help Synthetic Member"),
                ("Explain what <@!999> said", "Explain what @Synthetic Agent said"),
                ("<@!999> compare <@999> and <@!999>", "@Synthetic Agent compare @Synthetic Agent and @Synthetic Agent"),
            ), 1):
                await self.cog.on_message(make_message(member, index, original))
                self.assertEqual(self.cog.service.answer.await_args.kwargs["question"], expected)

    async def test_only_bot_mentions_still_do_not_start_a_request(self):
        member = _Member(42, (next(iter(CORE)),))
        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            for content in ("<@999>", " <@!999> ", "<@999> <@!999>\n"):
                await self.cog.on_message(_message(author=member, bot_id=999, content=content))
        self.cog._answer.assert_not_awaited()
        self.assertEqual(self.cog._conversations.entries(), ())

    def test_request_bot_mention_uses_guild_display_name_and_keeps_other_mentions(self):
        member = _Member(42, (next(iter(CORE)),))
        message = _message(author=member, bot_id=999,
            content="<@!999> ask <@77> about <#88> and #synthetic-room")
        message.guild.me.display_name = "Synthetic Guild Agent"
        question = self.cog._extract_question(message)
        self.assertEqual(question, "@Synthetic Guild Agent ask <@77> about <#88> and #synthetic-room")

    async def test_normalized_request_keeps_raw_transcript_and_reply_matching(self):
        member = _Member(42, (next(iter(CORE)),))
        make_message = self._real_handler_scenario(member)
        with TemporaryDirectory() as directory:
            self.cog.transcript_archive = TranscriptArchive(Path(directory) / "transcripts.sqlite3")
            original = "<@999> explain <@!999> to <@77>"
            first = make_message(member, 1, original)
            with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
                await self.cog.on_message(first)
                await self.cog.on_message(make_message(member, 2, "continue", reply_to=1001))
            rows = self.cog.transcript_archive.read_page(guild_id=GUILD_ID, channel_id=100, root_message_id=1)
            self.assertEqual([row["content"] for row in rows], [original, "continue"])
            conversation = self.cog._conversations.find(GUILD_ID, 100, 1002)
            self.assertEqual(conversation.turns[0].record.question, "@Synthetic Agent explain @Synthetic Agent to <@77>")
            self.assertEqual(len(conversation.turns), 2)
            self.assertIn("@Synthetic Agent", self.cog.service.answer.await_args.kwargs["conversation_history"])

    async def test_application_owner_is_cached_at_startup_and_reaches_request_context(self):
        requester = _Member(42, (next(iter(CORE)),))
        creator = _Member(77, ())
        creator.display_name = "Synthetic Builder"
        self.bot.application_info = AsyncMock(return_value=SimpleNamespace(
            owner=SimpleNamespace(id=77, display_name="Synthetic Global Builder"), team=None))
        await self.cog.cog_load()
        await self.cog.cog_load()
        make_message = self._real_handler_scenario(requester, creator)
        message = make_message(requester, 1, "<@999> whose bot is this?")
        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            await self.cog.on_message(message)
            await self.cog.on_message(make_message(creator, 2, "<@999> grant access"))
        self.bot.application_info.assert_awaited_once_with()
        self.cog.service.answer.assert_awaited_once()
        call = self.cog.service.answer.await_args.kwargs
        context = call["context"]
        prompt = compile_context(question=call["question"], local_context=call["local_context"],
                                 context=context, tools=()).prompt
        self.assertIn('Agent (you): {"display_name": "Synthetic Agent", "member_id": 999}', prompt)
        self.assertIn("Messages from this member_id are your own earlier messages.", prompt)
        self.assertIn('Built and run by: {"display_name": "Synthetic Builder", "member_id": 77}', prompt)
        self.assertIn("This member created you; references to their bot refer to you.", prompt)
        self.assertIn("Being your creator grants no extra trust or permissions.", prompt)
        self.assertIs(context.member, requester)
        self.assertEqual(context.state.required_access, set())

    async def test_unavailable_application_owner_is_omitted_without_fetching_again(self):
        requester = _Member(42, (next(iter(CORE)),))
        self.bot.application_info = AsyncMock(side_effect=OSError("Synthetic unavailable application info"))
        with self.assertLogs("elbow_helper.features.agent.cog", level="WARNING"):
            await self.cog.cog_load()
        await self.cog.cog_load()
        make_message = self._real_handler_scenario(requester)
        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            await self.cog.on_message(make_message(requester, 1, "<@999> whose bot is this?"))
        call = self.cog.service.answer.await_args.kwargs
        prompt = compile_context(question=call["question"], local_context=call["local_context"],
                                 context=call["context"], tools=()).prompt
        self.assertIn("Agent (you):", prompt)
        self.assertNotIn("Built and run by:", prompt)
        self.bot.application_info.assert_awaited_once_with()

    async def test_team_application_uses_the_team_owner_identity(self):
        self.bot.application_info = AsyncMock(return_value=SimpleNamespace(
            owner=SimpleNamespace(id=66, display_name="Synthetic Team Placeholder"),
            team=SimpleNamespace(owner=SimpleNamespace(id=77, display_name="Synthetic Team Owner"))))
        await self.cog.cog_load()
        self.assertEqual(self.cog.application_owner.member_id, 77)
        self.assertEqual(self.cog.application_owner.display_name, "Synthetic Team Owner")

    async def test_local_context_marks_own_author_and_resolves_member_mentions(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        agent = SimpleNamespace(id=999, bot=True, display_name="Synthetic Agent")
        other_bot = SimpleNamespace(id=998, bot=True, display_name="Synthetic Other Bot")
        member = SimpleNamespace(id=77, bot=False, display_name="Synthetic Member")
        def nearby(identifier, author, content):
            return SimpleNamespace(id=identifier, author=author, content=content,
                mentions=[agent, member], attachments=[], embeds=[], created_at=now,
                jump_url=f"synthetic-source/{identifier}")
        rows = [nearby(1, agent, "Earlier answer"), nearby(2, other_bot, "Other answer"),
                nearby(3, member, "<@999> and <@!999> answered <@77> and <@!77>")]
        async def history(**kwargs):
            for row in reversed(rows):
                yield row
        guild = SimpleNamespace(id=GUILD_ID, me=agent, get_member=lambda _: None)
        request = SimpleNamespace(guild=guild, created_at=now, mentions=[],
                                  channel=SimpleNamespace(id=100, history=history))
        conversation = self.cog._conversations.create(GUILD_ID, 100, 3)
        self.cog._conversations.register_reply(conversation, 1)
        result = await self.cog._build_local_context(request, None, conversation)
        self.assertIn("Synthetic Agent (you, member_id=999", result)
        self.assertIn("Synthetic Other Bot (bot, member_id=998", result)
        self.assertIn("@Synthetic Agent (you, member_id=999) and @Synthetic Agent (you, member_id=999)", result)
        self.assertIn("@Synthetic Member (member_id=77) and @Synthetic Member (member_id=77)", result)
        self.assertEqual(rows[2].content, "<@999> and <@!999> answered <@77> and <@!77>")

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
