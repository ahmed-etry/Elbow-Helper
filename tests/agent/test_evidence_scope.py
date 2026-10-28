"""Synthetic destination scopes; no live Discord or model calls."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from agent.engine.helpers import LegacyCatalogueMixin

import json
from types import SimpleNamespace
from elbow_helper.agent.wording import AGENT_ANSWER_UNFINISHED
import unittest
from unittest.mock import AsyncMock

import discord

from elbow_helper.configuration.channels import RECRUITMENT_TICKET_CATEGORY, SUPPORT_TICKET_CATEGORY
from elbow_helper.configuration.roles import AGENT_TESTER_ROLE_ID, LEAD, RECRUITERS
from elbow_helper.agent.access import AgentAccessLost, assert_answer_scope
from elbow_helper.agent.actions.answer_view import PrivateAnswerView
from elbow_helper.agent.delivery import AgentDeliveryMixin
from elbow_helper.agent.disclosure import request_scope, log_scope
from elbow_helper.agent.knowledge.answers import scoped_answer, private_answer_pass
from elbow_helper.agent.knowledge.store import KnowledgeCatalog, KnowledgeSection
from elbow_helper.agent.knowledge.tools import search_approved_knowledge
from elbow_helper.agent.models import AgentRequestContext, AgentTurnState, AgentDelivery
from elbow_helper.agent.research.history import read_message_context, searchable_channel_ids
from elbow_helper.agent.wording import AGENT_PRIVATE_OFFER, AGENT_PRIVATE_CHECK_BUTTON


@dataclass(frozen=True)
class Role:
    id: int
    managed: bool = False


class Channel:
    type = discord.ChannelType.text

    def __init__(self, identifier, guild, viewers, *, level=None, category=None):
        self.id, self.guild, self.name = identifier, guild, str(identifier)
        self.viewers = set(viewers)
        self.category_id = category
        self.overwrites = ({Role(next(iter(level))): discord.PermissionOverwrite(view_channel=True)}
                           if level else {Role(2): discord.PermissionOverwrite(view_channel=True)})
        self.reads = 0
        self.send = AsyncMock(return_value=SimpleNamespace(id=888))

    def permissions_for(self, actor):
        allowed = actor.id in self.viewers
        return SimpleNamespace(view_channel=allowed, read_message_history=allowed)

    async def history(self, **kwargs):
        self.reads += 1
        yield SimpleNamespace(id=123, author=SimpleNamespace(id=10, display_name="Applicant"),
                              channel=self, content="Synthetic recruitment evidence", embeds=[],
                              attachments=[], reference=None, guild=self.guild,
                              created_at=datetime.now(timezone.utc))


class ScopeTests(LegacyCatalogueMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.asker = SimpleNamespace(id=10, roles=[Role(AGENT_TESTER_ROLE_ID), Role(next(iter(RECRUITERS)))],
                                     bot=False, display_name="Asker")
        self.peer = SimpleNamespace(id=11, roles=[Role(next(iter(RECRUITERS)))], bot=False)
        self.ordinary = SimpleNamespace(id=12, roles=[], bot=False)
        self.bot_member = SimpleNamespace(id=99, roles=[], bot=True)
        members = [self.asker, self.peer, self.ordinary, self.bot_member]
        self.guild = SimpleNamespace(id=1, name="Synthetic guild", chunked=True, members=members, me=self.bot_member,
                                     roles=[Role(1), Role(2), *(Role(r) for r in RECRUITERS | LEAD)],
                                     default_role=Role(1), owner_id=10, threads=[])
        self.guild.get_member = lambda identifier: next((m for m in members if m.id == identifier), None)
        self.rec_room = Channel(100, self.guild, {10, 11, 99}, level=RECRUITERS)
        self.member_chat = Channel(101, self.guild, {10, 11, 12, 99})
        self.ticket = Channel(200, self.guild, {10, 99}, category=RECRUITMENT_TICKET_CATEGORY)
        self.personal = Channel(201, self.guild, {10, 99}, category=SUPPORT_TICKET_CATEGORY)
        self.guild.channels = [self.rec_room, self.member_chat, self.ticket, self.personal]
        self.guild.get_channel_or_thread = lambda identifier: next(
            (c for c in self.guild.channels if c.id == identifier), None)
        self.message = SimpleNamespace(id=1000, channel=self.rec_room, guild=self.guild, author=self.asker,
                                       mentions=[], reference=None, archive_reply=False,
                                       created_at=datetime.now(timezone.utc), reply=AsyncMock(return_value=SimpleNamespace(id=999)))
        self.context = AgentRequestContext(bot=SimpleNamespace(user=self.bot_member), guild=self.guild,
                                           member=self.asker, source_message=self.message,
                                           account_links=None, message_search=None)
        self.delivery = AgentDeliveryMixin()
        self.delivery.bot = self.context.bot

    async def test_delivery_unwraps_answer_only_envelopes_and_blocks_narration(self):
        for text in ('{"answer":"North meets BE4\'s bar."}',
                     '```json\n{"answer":"North meets BE4\'s bar."}\n```'):
            self.message.reply.reset_mock()
            await self.delivery.send_response(self.message, text, None, context=self.context)
            self.assertEqual(self.message.reply.await_args.args[0], "North meets BE4's bar.")
        self.message.reply.reset_mock()
        await self.delivery.send_response(self.message, json.dumps({
            "answer": "The ticket room itself isn't visible in exam-room.",
        }), None, context=self.context)
        sent = self.message.reply.await_args.args[0]
        self.assertNotIn("ticket room", sent)
        self.assertNotIn('"answer"', sent)

    async def test_recruiter_evidence_is_read_and_public_despite_missing_ticket_membership(self):
        result = await read_message_context(self.context, {"channel_id": 200, "message_id": 123})
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(self.ticket.reads, 1)
        active = AgentDelivery()
        await self.delivery.send_response(self.message, "Recruitment result", None, context=self.context, delivery=active)
        self.assertEqual(active.text_parts, ["Recruitment result"])
        self.assertNotIn("view", self.message.reply.await_args.kwargs)

    async def test_member_destination_never_reads_recruitment_and_offers_click_only_pass(self):
        self.message.channel = self.member_chat
        result = await read_message_context(self.context, {"channel_id": 200, "message_id": 123})
        self.assertIn("error", result)
        self.assertEqual(self.ticket.reads, 0)
        self.assertNotIn(200, await searchable_channel_ids(self.context))
        scoped = scoped_answer(self.context, json.dumps({"needs_private_evidence": True, "answer": "Public facts."}))
        self.context.state.private_answer_run = AsyncMock(return_value=("Private facts.", (), replace(
            self.context, state=AgentTurnState(), private_pass=True)))
        await self.delivery.send_response(self.message, scoped, None, context=self.context)
        self.assertEqual(self.message.reply.await_args.args[0], "Public facts.\n\n" + AGENT_PRIVATE_OFFER)
        self.context.state.private_answer_run.assert_not_awaited()
        view = self.message.reply.await_args.kwargs["view"]
        self.assertIsInstance(view, PrivateAnswerView)
        self.assertEqual([b.label for b in view.children], [AGENT_PRIVATE_CHECK_BUTTON])
        interaction = SimpleNamespace(user=self.asker, response=SimpleNamespace(defer=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock()))
        await view.check_privately(interaction)
        self.context.state.private_answer_run.assert_awaited_once()
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        self.assertEqual(self.message.reply.await_count, 1)

    async def test_lead_evidence_is_public_in_lead_destination(self):
        role = Role(next(iter(LEAD)))
        self.asker.roles.append(role)
        self.peer.roles.append(role)
        lead_source = Channel(300, self.guild, {10, 99}, level=LEAD)
        self.guild.channels.append(lead_source)
        result = await read_message_context(self.context, {"channel_id": 300, "message_id": 123})
        self.assertIn("messages", result)
        await self.delivery.send_response(self.message, "Lead result", None, context=self.context)
        self.assertEqual(self.message.reply.await_args.args[0], "Lead result")

    async def test_personal_ticket_is_not_read_until_every_destination_reader_can_read_it(self):
        result = await read_message_context(self.context, {"channel_id": 201, "message_id": 123})
        self.assertIn("error", result)
        self.assertEqual(self.personal.reads, 0)
        self.personal.viewers.add(11)
        self.assertIn("messages", await read_message_context(self.context, {"channel_id": 201, "message_id": 123}))

    async def test_guard_aborts_out_of_scope_citation_and_retained_evidence(self):
        for text, sources in (("See https://discord.com/channels/1/201/123", set()), ("text", {201})):
            self.context.state.source_channels = sources
            with self.assertRaises(AgentAccessLost):
                await self.delivery.send_response(self.message, text, None, context=self.context)
        self.message.reply.assert_not_awaited()

    async def test_guard_refreshes_destination_audience(self):
        await read_message_context(self.context, {"channel_id": 200, "message_id": 123})
        self.rec_room.viewers.add(12)
        with self.assertRaises(AgentAccessLost):
            await assert_answer_scope(self.context, "Recruitment result")

    async def test_knowledge_scope_never_returns_excluded_titles_or_bodies(self):
        self.message.channel = self.member_chat
        catalog = KnowledgeCatalog("now", (KnowledgeSection("Public", "Public facts"),
            KnowledgeSection("Recruitment policy", "Secret body", "recruiter")))
        context = replace(self.context, knowledge_store=SimpleNamespace(load=lambda: catalog))
        result = await search_approved_knowledge(context, {"query": "Recruitment policy", "scope": "requester"})
        self.assertEqual(result["sections"], [])
        self.assertTrue(result["available_privately"])
        self.assertNotIn("Secret body", json.dumps(result))
        self.assertNotIn("Recruitment policy", json.dumps(result))
        self.assertFalse(context.state.required_access)
        private = replace(context, private_pass=True, state=AgentTurnState())
        opened = await search_approved_knowledge(private, {"query": "Recruitment policy"})
        self.assertEqual(opened["sections"][0]["body"], "Secret body")

    async def test_extra_sources_without_materiality_do_not_offer(self):
        self.context.state.scope_audit.private_candidates.add("200")
        self.assertEqual(scoped_answer(self.context, "Public answer"), "Public answer")
        self.assertFalse(self.context.state.scope_audit.offered)

    async def test_offer_text_and_control_both_require_a_callable_private_check(self):
        self.context.state.scope_audit.private_candidates.add("201")
        self.context.state.scope_audit.offered = True
        await self.delivery.send_response(self.message, "Available facts.", None, context=self.context)
        self.assertEqual(self.message.reply.await_args.args[0], "Available facts.")
        self.assertNotIn("view", self.message.reply.await_args.kwargs)

    async def test_delivery_never_dumps_raw_findings_after_an_unsupported_private_claim(self):
        self.context.state.grounding_evidence["members"] = {"members": [{
            "display_name": "North", "joined_at": "2024-01-02T00:00:00+00:00"}]}
        await self.delivery.send_response(
            self.message, "The names and dates only open privately here.", None, context=self.context,
        )
        answer = self.message.reply.await_args.args[0]
        self.assertEqual(answer, AGENT_ANSWER_UNFINISHED)
        self.assertNotIn("2024-01-02", answer)
        self.assertNotIn("privately", answer)
        self.assertNotIn("view", self.message.reply.await_args.kwargs)

    async def test_scope_signal_preserves_multiline_answers_and_accepts_false_materiality(self):
        self.context.state.scope_audit.excluded["201"] = "per_person"
        self.context.state.scope_audit.private_candidates.add("201")
        answer = "Available applicant facts.\n\nThe interview notes aren't available in this channel."
        signal = '{"needs_private_evidence":true,"answer":"' + answer + '"}'
        self.assertEqual(scoped_answer(self.context, signal), answer)
        self.assertTrue(self.context.state.scope_audit.offered)
        self.context.state.scope_audit.offered = False
        self.assertEqual(scoped_answer(self.context, json.dumps({
            "needs_private_evidence": False, "answer": answer,
        })), answer)
        self.assertFalse(self.context.state.scope_audit.offered)

    async def test_starved_scope_never_turns_bad_envelopes_or_empty_answers_into_retry_errors(self):
        from elbow_helper.agent.wording import FAILURE_MESSAGE
        self.context.state.scope_audit.excluded["201"] = "per_person"
        for signal in (FAILURE_MESSAGE, "", '{"needs_private_evidence":',
                       '{"needs_private_evidence":true,"reason":"internal marker"}',
                       json.dumps({"needs_private_evidence": True, "answer": FAILURE_MESSAGE})):
            with self.subTest(signal=signal):
                self.assertEqual(scoped_answer(self.context, signal), AGENT_ANSWER_UNFINISHED)
        self.assertFalse(self.context.state.scope_audit.offered)

    async def test_service_and_delivery_keep_scope_empty_distinct_from_request_failure(self):
        from unittest.mock import patch
        from elbow_helper.agent.engine.service import AgentService
        from elbow_helper.agent.wording import FAILURE_MESSAGE
        self.context.state.scope_audit.excluded["201"] = "per_person"
        service = AgentService(None)
        with patch.object(service, "_answer", AsyncMock(return_value=FAILURE_MESSAGE)):
            answer = await service.answer(question="Read the interview", local_context="", context=self.context)
        self.assertEqual(answer, AGENT_ANSWER_UNFINISHED)
        await self.delivery.send_response(self.message, FAILURE_MESSAGE, None, context=self.context)
        self.assertEqual(self.message.reply.await_args.args[0], AGENT_ANSWER_UNFINISHED)

    async def test_private_pass_uses_fresh_state_and_current_clearance(self):
        self.message.channel = self.member_chat
        await request_scope(self.context)
        self.context.state.evidence.append("public evidence")
        async def answer(**kwargs):
            private = kwargs["context"]
            self.assertTrue(private.private_pass)
            self.assertFalse(private.state.evidence)
            self.assertIsNone(private.send_reply)
            self.assertIn("recruiter", (await request_scope(private)).levels)
            self.assertIn("messages", await read_message_context(private, {"channel_id": 200, "message_id": 123}))
            return "Private answer"
        service = SimpleNamespace(answer=AsyncMock(side_effect=answer))
        result = await private_answer_pass(service, self.context, "Question", "Public context")
        self.assertEqual(result[0], "Private answer")
        self.assertNotIn(200, self.context.state.source_channels)
        self.assertEqual(self.context.state.evidence, ["public evidence"])

    async def test_button_checks_owner_and_revoked_clearance(self):
        self.context.state.private_answer_run = AsyncMock()
        view = PrivateAnswerView(self.context)
        interaction = SimpleNamespace(user=self.peer, response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock()))
        await view.check_privately(interaction)
        self.context.state.private_answer_run.assert_not_awaited()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_audit_records_scope_exclusions_and_offer(self):
        self.message.channel = self.member_chat
        await read_message_context(self.context, {"channel_id": 200, "message_id": 123})
        scoped_answer(self.context, '{"needs_private_evidence":true,"answer":"Public facts"}')
        with self.assertLogs("elbow_helper.agent.disclosure", level="INFO") as captured:
            log_scope(self.context)
        self.assertIn("clearance=[] scope=[]", captured.output[0])
        self.assertIn("role_scope", captured.output[0])
        self.assertIn("asker_only_offered=True", captured.output[0])

    async def test_recruiter_role_authorizes_ticket_without_requester_overwrite(self):
        self.ticket.viewers.remove(self.asker.id)
        result = await read_message_context(self.context, {"channel_id": 200, "message_id": 123})
        self.assertIn("messages", result)
        await assert_answer_scope(self.context, "Recruitment facts")

    async def test_guard_covers_alternate_discord_citation_hosts(self):
        for url in ("http://discordapp.com/channels/1/201/123",
                    "https://canary.discord.com/channels/1/201/123",
                    "https://ptb.discord.com/channels/1/201"):
            with self.subTest(url=url), self.assertRaises(AgentAccessLost):
                await assert_answer_scope(self.context, url)

    async def test_service_and_button_run_only_scoped_passes(self):
        from unittest.mock import patch
        from elbow_helper.agent.engine.service import AgentService
        from elbow_helper.agent.engine.registry import build_agent_tools
        from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
        from agent.engine.test_agent_plan_flow import _Model, _Session, _model_step, _plan
        self.message.channel = self.member_chat
        self.message.content = "Check the recruitment ticket"
        step = {"id": "ticket", "capability": "read_message_context",
                "arguments": {"channel_id": 200, "message_id": 123}, "depends_on": []}
        public = _Session([_model_step(_plan([step])), AgentStep(
            '{"needs_private_evidence":true,"answer":"Public facts"}', (), AgentUsage())], [])
        private = _Session([_model_step(_plan([step])), AgentStep("Private facts", (), AgentUsage())], [])
        model = _Model(public)
        service = AgentService(model)
        tool = replace(build_agent_tools()["read_message_context"], default_visible=True)
        with patch("elbow_helper.agent.engine.service.build_agent_tools", return_value={"read_message_context": tool}):
            answer = await service.answer(question="Check the recruitment ticket", local_context="", context=self.context)
            self.assertEqual(answer, "Public facts")
            self.assertEqual(self.ticket.reads, 0)
            self.assertTrue(self.context.state.scope_audit.offered)
            self.assertFalse(private.calls)
            self.assertNotIn("Synthetic recruitment evidence", str(public.calls))
            await self.delivery.send_response(self.message, answer, None, context=self.context)
            view = self.message.reply.await_args.kwargs["view"]
            self.addCleanup(view.stop)
            model.session = private
            interaction = SimpleNamespace(user=self.asker, response=SimpleNamespace(defer=AsyncMock()),
                                          followup=SimpleNamespace(send=AsyncMock()))
            await view.check_privately(interaction)
            self.assertEqual(self.ticket.reads, 1)
            self.assertIn("Synthetic recruitment evidence", str(private.calls))
            self.assertEqual(interaction.followup.send.await_args.args[0], "Private facts")
            self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
            self.assertNotIn(200, self.context.state.source_channels)
            self.asker.roles[:] = [Role(AGENT_TESTER_ROLE_ID)]
            await view.check_privately(interaction)
            from elbow_helper.agent.wording import ACTION_RESULT_EXPIRED
            self.assertEqual(interaction.followup.send.await_args.args[0], ACTION_RESULT_EXPIRED)
            self.assertEqual(self.ticket.reads, 1)
