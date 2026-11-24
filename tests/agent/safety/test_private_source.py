"""Destination-only conversation refusals keep the existing private read path."""

from contextlib import ExitStack
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from elbow_helper.agent.actions.answer_view import PostAnswerView
from elbow_helper.agent.engine.models import AgentTurnState
from elbow_helper.agent.research.history import read_discord_channel_history, read_message_context
from elbow_helper.agent.research.search import search_discord_messages
from elbow_helper.agent.conversation.wording import AGENT_PRIVATE_OFFER, AGENT_PRIVATE_CHECK_BUTTON, RESEARCH_FEEDBACK_HINT
from elbow_helper.infrastructure.discord.message_search import DiscordHistoryPage, DiscordSearchMessage, DiscordSearchPage
from tests.agent.safety import test_evidence_scope as fixtures
from tests.agent.loop_stub import StubModel, StubSession, call, final, loop_service, without_server_notes


class PrivateSourceTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ScopeTests.setUp

    async def test_loop_offers_the_full_answer_only_when_the_model_calls_the_offer(self):
        read = ("read_conversation_evidence", {"mode": "message_window", "channel_id": 201, "message_id": 123})
        public_text = "That conversation is not available here."
        for offers in (True, False):
            with self.subTest(offers=offers), ExitStack() as stack:
                without_server_notes(stack)
                self.context = replace(self.context, state=AgentTurnState())
                self.message.reply.reset_mock()
                self.personal.reads = 0
                public = StubSession(call(*read), *((call("offer_full_answer", {}, "call-2"),) if offers else ()),
                                     final(public_text))
                model = StubModel(public)
                service = loop_service(model)
                answer = await service.answer(question="What did the applicant ask in that conversation?",
                                              context=self.context)
                self.assertEqual(answer, public_text)
                audit = self.context.state.scope_audit
                self.assertEqual(audit.private_candidates, {"201"})
                self.assertEqual(audit.offered, offers)
                # The offer is listed from the start, so the tool list never changes mid-request.
                self.assertEqual(public.tool_names, [])
                self.assertIn("offer_full_answer", [tool.name for tool in model.requests[0]["tools"]])
                self.assertEqual(self.personal.reads, 0)
                self.assertNotIn("Synthetic recruitment evidence", str(public.calls))
                await self.delivery.send_response(self.message, answer, None, context=self.context)
                if not offers:
                    self.assertEqual(self.message.reply.await_args.args[0], public_text + "\n" + RESEARCH_FEEDBACK_HINT)
                    self.assertNotIn("view", self.message.reply.await_args.kwargs)
                    continue
                self.assertEqual(self.message.reply.await_args.args[0], public_text + "\n\n" + AGENT_PRIVATE_OFFER)
                view = self.message.reply.await_args.kwargs["view"]
                self.addCleanup(view.stop)
                self.assertEqual([button.label for button in view.children], [AGENT_PRIVATE_CHECK_BUTTON])
                private = StubSession(call(*read), final("Synthetic recruitment evidence"))
                model.sessions.append(private)
                interaction = SimpleNamespace(user=self.asker, response=SimpleNamespace(defer=AsyncMock()),
                                              followup=SimpleNamespace(send=AsyncMock()))
                await view.check_privately(interaction)
                # Pressing the button re-ran the question at the asker's access, with no offer to make.
                self.assertEqual(self.personal.reads, 1)
                self.assertEqual(interaction.followup.send.await_args.args[0], "Synthetic recruitment evidence")
                self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
                post_view = interaction.followup.send.await_args.kwargs["view"]
                self.addCleanup(post_view.stop)
                self.assertIsInstance(post_view, PostAnswerView)
                # A private pass can never offer, so it isn't listed there.
                self.assertNotIn("offer_full_answer", [tool.name for tool in model.requests[1]["tools"]])
                self.assertEqual(private.tool_names, [])
                self.assertNotIn(201, self.context.state.source_channels)
                self.assertIn(201, view.result[2].state.source_channels)

    async def test_loop_offer_is_refused_without_a_held_back_source_or_in_a_private_pass(self):
        with ExitStack() as stack:
            without_server_notes(stack)
            for private_pass in (False, True):
                with self.subTest(private_pass=private_pass):
                    context = replace(self.context, state=AgentTurnState(), private_pass=private_pass)
                    session = StubSession(call("offer_full_answer", {}), final("Synthetic answer"))
                    await loop_service(StubModel(session)).answer(question="Synthetic question", context=context)
                    self.assertEqual(json.loads(session.calls[1][0].content), {"error": "unavailable_lookup"})
                    self.assertFalse(context.state.scope_audit.offered)

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
                    session = StubSession(call("read_conversation_evidence", {
                        "mode": "message_window", "channel_id": 201, "message_id": 123}), final(refusal))
                    with ExitStack() as stack:
                        without_server_notes(stack)
                        answer = await loop_service(StubModel(session)).answer(
                            question="What did the applicant ask in that conversation?", context=self.context)
                    self.assertEqual(answer, refusal)
                    result = json.loads(session.calls[1][0].content)
                    result.pop("flags", None)
                    self.assertEqual(result, {"error": refusal})
                    self.assertFalse(self.context.state.scope_audit.private_candidates)
                    self.assertFalse(self.context.state.scope_audit.offered)
                    await self.delivery.send_response(self.message, answer, None, context=self.context)
                    self.assertEqual(self.message.reply.await_args.args[0], refusal + "\n" + RESEARCH_FEEDBACK_HINT)
                    self.assertNotIn("view", self.message.reply.await_args.kwargs)
                    private = replace(self.context, private_pass=True, state=AgentTurnState())
                    self.assertEqual(await read_message_context(private, {"channel_id": 201, "message_id": 123}),
                                     {"error": refusal})
                    self.assertEqual(self.personal.reads, 0)
                finally:
                    self.personal.permissions_for = original


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
                    if "channel_ids" in arguments:
                        self.assertNotIn("error", result)
                        self.assertEqual(result["matches"], [])
                        self.assertEqual(result["missing_sources"], [
                            {"channel_id": 201, "cause": "source_unavailable"}])
                        self.assertNotIn("total_results_estimate", result["coverage"])
                    else:
                        self.assertIn("error", result)
                        self.assertEqual(bool(result.get("available_privately")), revoked is self.peer)
                    self.assertNotIn("Synthetic recruitment evidence", json.dumps(result))
                    self.assertNotIn(201, self.context.state.source_channels)

    async def test_failed_channel_fetch_discards_an_earlier_private_candidate(self):
        await read_message_context(self.context, {"channel_id": 201, "message_id": 123})
        self.guild.channels.remove(self.personal)
        self.context.bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Missing channel"))
        result = await read_message_context(self.context, {"channel_id": 201, "message_id": 123})
        self.assertEqual(result, {"error": "That conversation is not available here."})
        self.assertFalse(self.context.state.scope_audit.private_candidates)
