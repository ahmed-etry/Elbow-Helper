"""Destination-only conversation refusals keep the existing private read path."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from elbow_helper.agent.actions.answer_view import PrivateAnswerView, PostAnswerView
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.steps import disclosure_issue
from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.engine.evidence_delta import access_refused
from elbow_helper.agent.models import AgentTurnState
from elbow_helper.agent.research.history import read_discord_channel_history, read_message_context
from elbow_helper.agent.research.search import search_discord_messages
from elbow_helper.agent.wording import AGENT_PRIVATE_OFFER, AGENT_PRIVATE_CHECK_BUTTON
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from elbow_helper.infrastructure.discord.message_search import (
    DiscordHistoryPage, DiscordSearchMessage, DiscordSearchPage,
)
from agent import test_evidence_scope as fixtures
from agent.engine.test_agent_plan_flow import _Model, _Session, _model_step, _plan


class PrivateSourceTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ScopeTests.setUp

    def session(self, answer):
        step = {"id": "conversation", "capability": "read_conversation_evidence",
                "arguments": {"mode": "message_window", "channel_id": 201, "message_id": 123},
                "depends_on": []}
        return _Session([_model_step(_plan([step])), AgentStep(answer, (), AgentUsage())], [])

    async def answer(self, service):
        return await service.answer(question="What did the applicant ask in that conversation?",
                                    local_context="", context=self.context)

    async def test_destination_only_refusal_offers_content_through_existing_private_pass(self):
        public_text = "That conversation is not available here."
        public = self.session(json.dumps({"needs_private_evidence": True, "answer": public_text}))
        model = _Model(public)
        service = AgentService(model)
        answer = await self.answer(service)
        self.assertEqual(answer, public_text)
        result = json.loads(public.calls[1][0][0].content)["results"]["conversation"]
        result.pop("flags", None)
        self.assertEqual(result, {"error": public_text, "cause": "access_denied", "available_privately": True})
        self.assertEqual(self.context.state.scope_audit.excluded, {"201": "per_person"})
        self.assertEqual(self.context.state.scope_audit.private_candidates, {"201"})
        self.assertTrue(self.context.state.scope_audit.offered)
        self.assertEqual(self.personal.reads, 0)
        self.assertNotIn("Synthetic recruitment evidence", str(public.calls))

        await self.delivery.send_response(self.message, answer, None, context=self.context)
        self.assertEqual(self.message.reply.await_args.args[0], public_text + "\n\n" + AGENT_PRIVATE_OFFER)
        view = self.message.reply.await_args.kwargs["view"]
        self.addCleanup(view.stop)
        self.assertIsInstance(view, PrivateAnswerView)
        self.assertEqual([button.label for button in view.children], [AGENT_PRIVATE_CHECK_BUTTON])
        private = self.session("Synthetic recruitment evidence")
        model.session = private
        interaction = SimpleNamespace(user=self.asker, response=SimpleNamespace(defer=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock()))
        await view.check_privately(interaction)
        self.assertEqual(self.personal.reads, 1)
        self.assertIn("Synthetic recruitment evidence", str(private.calls))
        self.assertEqual(interaction.followup.send.await_args.args[0], "Synthetic recruitment evidence")
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        post_view = interaction.followup.send.await_args.kwargs["view"]
        self.addCleanup(post_view.stop)
        self.assertIsInstance(post_view, PostAnswerView)
        self.assertNotIn(201, self.context.state.source_channels)
        self.assertIn(201, view.result[2].state.source_channels)
        self.assertEqual(self.message.reply.await_count, 1)

    async def test_real_permission_failures_keep_refusal_without_a_private_offer(self):
        for actor, denied_permission in ((self.bot_member, "read_message_history"),
                                         (self.asker, "view_channel"),
                                         (self.asker, "read_message_history")):
            with self.subTest(actor=actor.id, permission=denied_permission):
                self.context = replace(self.context, state=AgentTurnState())
                # An earlier audience refusal must not survive a real access loss.
                await read_message_context(self.context, {"channel_id": 201, "message_id": 123})
                original = self.personal.permissions_for

                def permissions(member):
                    value = original(member)
                    return SimpleNamespace(**{
                        field: getattr(value, field) and not (member.id == actor.id and field == denied_permission)
                        for field in ("view_channel", "read_message_history")})

                self.personal.permissions_for = permissions
                try:
                    refusal = "That conversation is not available here."
                    session = self.session(json.dumps({"needs_private_evidence": True, "answer": refusal}))
                    answer = await self.answer(AgentService(_Model(session)))
                    self.assertEqual(answer, refusal)
                    result = json.loads(session.calls[1][0][0].content)["results"]["conversation"]
                    result.pop("flags", None)
                    self.assertEqual(result, {"error": refusal})
                    self.assertFalse(self.context.state.scope_audit.private_candidates)
                    self.assertFalse(self.context.state.scope_audit.offered)
                    await self.delivery.send_response(self.message, answer, None, context=self.context)
                    self.assertEqual(self.message.reply.await_args.args[0], refusal)
                    self.assertNotIn("view", self.message.reply.await_args.kwargs)
                    private = replace(self.context, private_pass=True, state=AgentTurnState())
                    self.assertEqual(await read_message_context(private, {"channel_id": 201, "message_id": 123}),
                                     {"error": refusal})
                    self.assertEqual(self.personal.reads, 0)
                finally:
                    self.personal.permissions_for = original

    async def test_shared_destination_posts_the_read_content_as_before(self):
        self.personal.viewers.add(self.peer.id)
        session = self.session("Synthetic recruitment evidence")
        answer = await self.answer(AgentService(_Model(session)))
        self.assertEqual(self.personal.reads, 1)
        self.assertIn("Synthetic recruitment evidence", str(session.calls))
        self.assertFalse(self.context.state.scope_audit.private_candidates)
        self.assertFalse(self.context.state.scope_audit.offered)
        await self.delivery.send_response(self.message, answer, None, context=self.context)
        self.assertEqual(self.message.reply.await_args.args[0], "Synthetic recruitment evidence")
        self.assertNotIn("view", self.message.reply.await_args.kwargs)

    async def test_message_history_and_search_reads_signal_only_the_refused_source(self):
        readers = ((read_message_context, {"channel_id": 201, "message_id": 123}),
                   (read_discord_channel_history, {"channel_id": 201}),
                   (search_discord_messages, {"channel_id": 201}))
        for reader, arguments in readers:
            with self.subTest(reader=reader.__name__):
                result = await reader(self.context, arguments)
                self.assertEqual(result["cause"], "access_denied")
                self.assertTrue(result["available_privately"])
                self.assertTrue(access_refused(result))
                self.assertEqual(answer_evidence({"conversation": result})["conversation"],
                                 {"status": "denied", "available_privately": True})
                self.assertNotIn("Synthetic recruitment evidence", json.dumps(result))
                self.assertEqual(self.personal.reads, 0)
        self.ticket.viewers.remove(self.bot_member.id)
        registry = build_agent_tools()
        # Legacy channel-bound reads have a preflight; family reads check inside.
        denied = await disclosure_issue(self.context, registry, {
            "capability": "read_message_context", "arguments": {"channel_id": 200, "message_id": 123}})
        self.assertFalse(denied["available_privately"])
        self.assertEqual(denied["cause"], "unavailable")
        allowed_privately = await disclosure_issue(self.context, registry, {
            "capability": "read_message_context", "arguments": {"channel_id": 201, "message_id": 123}})
        self.assertTrue(allowed_privately["available_privately"])
        self.assertEqual(allowed_privately["cause"], "access_denied")

    async def test_access_changes_during_reads_discard_content_and_report_the_current_reason(self):
        original_history = self.personal.history
        row = DiscordSearchMessage(123, 201, 10, "Applicant", "Synthetic recruitment evidence",
                                   "2026-01-01T00:00:00+00:00")
        for revoked in (self.peer, self.bot_member):
            for reader, arguments in ((read_message_context, {"channel_id": 201, "message_id": 123}),
                                      (read_discord_channel_history, {"channel_id": 201}),
                                      (search_discord_messages, {"channel_id": 201}),
                                      (search_discord_messages, {"channel_ids": [201, 100]})):
                with self.subTest(reader=reader.__name__, arguments=arguments, revoked=revoked.id):
                    self.context = replace(self.context, state=AgentTurnState())
                    self.personal.viewers = {10, 11, 99}

                    async def history(**kwargs):
                        self.personal.viewers.remove(revoked.id)
                        async for message in original_history(**kwargs):
                            yield message

                    async def history_page(**kwargs):
                        self.personal.viewers.remove(revoked.id)
                        return DiscordHistoryPage((row,), kwargs["before_id"], kwargs["after_id"],
                                                  kwargs["limit"], None, True)

                    async def search_page(**kwargs):
                        self.personal.viewers.remove(revoked.id)
                        return DiscordSearchPage((row,), 0, 5, 1, None, False, False)

                    async def search(**kwargs):
                        self.personal.viewers.remove(revoked.id)
                        return (row,)

                    self.personal.history = history
                    self.context = replace(self.context, message_search=SimpleNamespace(
                        history_page=history_page, search_page=search_page, search=search))
                    result = await reader(self.context, arguments)
                    self.assertIn("error", result)
                    self.assertNotIn("Synthetic recruitment evidence", json.dumps(result))
                    self.assertNotIn(201, self.context.state.source_channels)
                    self.assertEqual(bool(result.get("available_privately")), revoked is self.peer)

    async def test_failed_channel_fetch_discards_an_earlier_private_candidate(self):
        await read_message_context(self.context, {"channel_id": 201, "message_id": 123})
        self.guild.channels.remove(self.personal)
        self.context.bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Missing channel"))
        result = await read_message_context(self.context, {"channel_id": 201, "message_id": 123})
        self.assertEqual(result, {"error": "That conversation is not available here."})
        self.assertFalse(self.context.state.scope_audit.private_candidates)
