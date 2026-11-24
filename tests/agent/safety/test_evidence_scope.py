"""Synthetic destination scopes; no live Discord or model calls."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from elbow_helper.configuration.channels import RECRUITMENT_TICKET_CATEGORY, SUPPORT_TICKET_CATEGORY
from elbow_helper.configuration.roles import AGENT_TESTER_ROLE_ID, LEAD, RECRUITERS
from elbow_helper.agent.safety.access import AgentAccessLost, assert_answer_scope
from elbow_helper.agent.actions.answer_view import PrivateAnswerView
from elbow_helper.agent.conversation.delivery import AgentDeliveryMixin
from elbow_helper.agent.safety.disclosure import request_scope
from elbow_helper.agent.knowledge.answers import private_answer_pass
from elbow_helper.agent.knowledge.store import KnowledgeCatalog, KnowledgeSection
from elbow_helper.agent.knowledge.tools import search_approved_knowledge
from elbow_helper.agent.engine.models import AgentRequestContext, AgentTurnState, AgentDelivery
from elbow_helper.agent.research.history import read_message_context


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


class ScopeTests(unittest.IsolatedAsyncioTestCase):
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


    async def test_recruiter_evidence_is_read_and_public_despite_missing_ticket_membership(self):
        result = await read_message_context(self.context, {"channel_id": 200, "message_id": 123})
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(self.ticket.reads, 1)
        active = AgentDelivery()
        await self.delivery.send_response(self.message, "Recruitment result", None, context=self.context, delivery=active)
        self.assertEqual(active.text_parts, ["Recruitment result"])
        self.assertNotIn("view", self.message.reply.await_args.kwargs)


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


    async def test_offer_text_and_control_both_require_a_callable_private_check(self):
        self.context.state.scope_audit.private_candidates.add("201")
        self.context.state.scope_audit.offered = True
        await self.delivery.send_response(self.message, "Available facts.", None, context=self.context)
        self.assertEqual(self.message.reply.await_args.args[0], "Available facts.")
        self.assertNotIn("view", self.message.reply.await_args.kwargs)

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


    async def test_guard_covers_alternate_discord_citation_hosts(self):
        for url in ("http://discordapp.com/channels/1/201/123",
                    "https://canary.discord.com/channels/1/201/123",
                    "https://ptb.discord.com/channels/1/201"):
            with self.subTest(url=url), self.assertRaises(AgentAccessLost):
                await assert_answer_scope(self.context, url)

    async def test_service_and_button_run_only_scoped_passes(self):
        from contextlib import ExitStack
        from tests.agent.loop_stub import StubModel, StubSession, call, final, loop_service, without_server_notes
        self.message.channel = self.member_chat
        self.message.content = "Check the recruitment ticket"
        read = ("read_message_context", {"channel_id": 200, "message_id": 123})
        public = StubSession(call(*read), call("offer_full_answer", {}, "call-2"), final("Public facts"))
        private = StubSession(call(*read), final("Private facts"))
        model = StubModel(public)
        with ExitStack() as stack:
            without_server_notes(stack)
            service = loop_service(model)
            answer = await service.answer(question="Check the recruitment ticket", context=self.context)
            self.assertEqual(answer, "Public facts")
            self.assertEqual(self.ticket.reads, 0)
            self.assertTrue(self.context.state.scope_audit.offered)
            self.assertFalse(private.calls)
            self.assertNotIn("Synthetic recruitment evidence", str(public.calls))
            await self.delivery.send_response(self.message, answer, None, context=self.context)
            view = self.message.reply.await_args.kwargs["view"]
            self.addCleanup(view.stop)
            model.sessions.append(private)
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
            from elbow_helper.agent.conversation.wording import ACTION_RESULT_EXPIRED
            self.assertEqual(interaction.followup.send.await_args.args[0], ACTION_RESULT_EXPIRED)
            self.assertEqual(self.ticket.reads, 1)
