"""Knowledge search avoids exposing unrelated restricted evidence to public turns."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.roles import AGENT_TESTER_ROLE_ID, LEAD
from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.knowledge.store import KnowledgeCatalog, KnowledgeSection
from elbow_helper.features.agent.knowledge.tools import knowledge_tools, search_approved_knowledge
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.wording import ACTION_PRIVATE_ANSWER
from tests.features.agent.test_agent_audiences import Channel, Member, Role, Thread


class SearchScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requester = Member(2, (Role(AGENT_TESTER_ROLE_ID), Role(next(iter(LEAD)))))
        self.reader = Member(3, (Role(AGENT_TESTER_ROLE_ID),))
        self.bot_member = Member(99, (), bot=True)
        self.members = [self.requester, self.reader, self.bot_member]
        self.guild = SimpleNamespace(
            id=1, chunked=True, members=self.members, me=self.bot_member,
            get_member=lambda identifier: next((m for m in self.members if m.id == identifier), None),
        )
        self.channel = Channel(10, self.guild, {2, 3, 99})
        self.message = SimpleNamespace(
            id=50, author=self.requester, guild=self.guild, channel=self.channel,
            created_at=datetime.now(timezone.utc), mentions=(), archive_reply=False,
            reply=AsyncMock(return_value=SimpleNamespace(id=51)),
        )
        self.catalog = KnowledgeCatalog("synthetic", (
            KnowledgeSection("Clan family lore", "Brown Elbow founding history", "public", "public.md"),
            KnowledgeSection("Alliance policies", "Brown Elbow founding history " * 200,
                             "lead", "lead.md"),
            KnowledgeSection("Returning applicants", "Brown Elbow founding history",
                             "recruiter", "recruiter.md"),
        ))
        self.context = AgentRequestContext(
            bot=SimpleNamespace(), guild=self.guild, member=self.requester,
            source_message=self.message, account_links=None, message_search=None,
            knowledge_store=SimpleNamespace(load=lambda: self.catalog),
        )
        self.delivery = AgentDeliveryMixin()
        self.delivery.bot = SimpleNamespace(user=self.bot_member)

    async def deliver(self):
        await self.delivery.send_response(self.message, "Synthetic answer", None, context=self.context)
        return self.message.reply.await_args.args[0]

    async def test_broad_public_search_never_ranks_restricted_sections_and_stays_public(self):
        ranked = []
        search = KnowledgeCatalog.search

        def record(catalog, sections, **arguments):
            ranked.extend(sections)
            return search(catalog, sections, **arguments)

        with patch.object(KnowledgeCatalog, "search", record):
            result = await search_approved_knowledge(
                self.context, {"query": "Brown Elbow founding history"},
            )
        self.assertEqual([s.visibility for s in ranked], ["public"])
        self.assertEqual([s["title"] for s in result["sections"]], ["Clan family lore"])
        self.assertEqual(result["scope"], "channel")
        self.assertEqual(self.context.state.required_access, set())
        self.assertEqual(await self.deliver(), "Synthetic answer")

    async def test_explicit_wider_search_retains_restrictions_and_delivers_private(self):
        result = await search_approved_knowledge(
            self.context, {"query": "Brown Elbow founding history", "scope": "requester"},
        )
        self.assertEqual({s["visibility"] for s in result["sections"]}, {"public", "lead", "recruiter"})
        self.assertEqual(result["scope"], "requester")
        self.assertEqual(self.context.state.required_access, {"lead", "recruiter"})
        # A later public title cannot undo restrictions from evidence already read.
        await search_approved_knowledge(self.context, {"titles": ["Clan family lore"]})
        self.assertEqual(self.context.state.required_access, {"lead", "recruiter"})
        with self.assertLogs("elbow_helper.features.agent.disclosure", "INFO") as logs:
            self.assertEqual(await self.deliver(), ACTION_PRIVATE_ANSWER)
        self.assertIn("failed_requirement=lead reader_kind=role_group", logs.output[0])

    async def test_exact_public_title_stays_public_and_is_the_documented_first_choice(self):
        result = await search_approved_knowledge(
            self.context, {"titles": ["Clan family lore"], "query": "Brown Elbow founding history"},
        )
        self.assertEqual([s["visibility"] for s in result["sections"]], ["public"])
        self.assertEqual(result["coverage"]["method"], "exact_titles")
        self.assertEqual(self.context.state.required_access, set())
        self.assertEqual(await self.deliver(), "Synthetic answer")
        self.assertIn("Open matching titles first;",
                      knowledge_tools()[0].definition.description)

    async def test_requester_scope_never_grants_roles_the_requester_lacks(self):
        self.members[0] = Member(2, (Role(AGENT_TESTER_ROLE_ID),))
        result = await search_approved_knowledge(self.context, {"scope": "requester"})
        self.assertEqual([s["visibility"] for s in result["sections"]], ["public"])
        self.assertEqual(self.context.state.required_access, set())

    async def test_restricted_titles_ids_and_continuations_need_explicit_wider_scope(self):
        title = {"titles": ["Alliance policies"]}
        hidden = await search_approved_knowledge(self.context, title)
        unknown = await search_approved_knowledge(self.context, {"titles": ["Unknown"]})
        self.assertEqual(hidden, unknown)
        first = await search_approved_knowledge(self.context, {**title, "scope": "requester"})
        section, = first["sections"]
        continuation = {"section_id": section["section_id"],
                        "section_offset": section["next_section_offset"]}
        self.assertTrue(section["truncated"])
        self.assertEqual((await search_approved_knowledge(self.context, continuation))["sections"], [])
        rest = await search_approved_knowledge(self.context, {**continuation, "scope": "requester"})
        self.assertEqual(section["body"] + rest["sections"][0]["body"], self.catalog.active_sections[1].body)
        self.assertEqual(self.context.state.required_access, {"lead"})

    async def test_channel_scope_uses_delivery_destination_and_ignores_bots_for_roles(self):
        destination = Channel(11, self.guild, {2, 99})
        self.context = replace(self.context, delivery_channel=destination)
        result = await search_approved_knowledge(self.context, {})
        self.assertEqual({s["visibility"] for s in result["sections"]}, {"public", "lead", "recruiter"})

    async def test_private_thread_includes_unjoined_moderators_and_fails_closed_on_unknown_members(self):
        destination = Thread(11, self.channel, private=True, members=[self.requester])
        self.context = replace(self.context, delivery_channel=destination)
        with patch("elbow_helper.features.agent.disclosure.discord.Thread", Thread):
            result = await search_approved_knowledge(self.context, {})
            self.assertIn("lead", {s["visibility"] for s in result["sections"]})
            destination.fetch_members.assert_awaited_once()
            self.channel.moderators.add(self.reader.id)
            self.context.state.required_access.clear()
            result = await search_approved_knowledge(self.context, {})
            self.assertEqual([s["visibility"] for s in result["sections"]], ["public"])
            self.channel.moderators.clear()
            self.context.disclosure_thread_members.clear()
            destination.fetch_members.return_value = [SimpleNamespace(id=404)]
            result = await search_approved_knowledge(self.context, {})
            self.assertEqual([s["visibility"] for s in result["sections"]], ["public"])

    async def test_invalid_scope_cannot_widen_search(self):
        result = await search_approved_knowledge(self.context, {"scope": "all"})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.required_access, set())
