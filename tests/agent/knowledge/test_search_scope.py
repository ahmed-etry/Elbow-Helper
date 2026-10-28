"""Knowledge search avoids exposing unrelated restricted evidence to public turns."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.roles import AGENT_TESTER_ROLE_ID, LEAD
from elbow_helper.agent.delivery import AgentDeliveryMixin
from elbow_helper.agent.knowledge.store import KnowledgeCatalog, KnowledgeSection
from elbow_helper.agent.knowledge.tools import knowledge_tools, search_approved_knowledge
from elbow_helper.agent.models import AgentRequestContext, AgentTurnState
from tests.agent.test_agent_audiences import Channel, Member, Role, Thread


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

    async def test_search_ranks_requester_sections_but_withholds_private_bodies(self):
        ranked = []
        search = KnowledgeCatalog.search

        def record(catalog, sections, **arguments):
            ranked.extend(sections)
            return search(catalog, sections, **arguments)

        with patch.object(KnowledgeCatalog, "search", record):
            result = await search_approved_knowledge(
                self.context, {"query": "Brown Elbow founding history"},
            )
        self.assertEqual({s.visibility for s in ranked}, {"public", "lead", "recruiter"})
        self.assertEqual([section["visibility"] for section in result["sections"]], ["public"])
        self.assertFalse(result["available_privately"])
        self.assertNotIn("Alliance policies", str(result))
        self.assertNotIn("Returning applicants", str(result))
        self.assertEqual(result["scope"], "channel")
        self.assertEqual(self.context.state.required_access, set())
        self.assertFalse(self.context.state.knowledge_title_access)
        self.assertEqual(await self.deliver(), "Synthetic answer")

    async def test_legacy_scope_cannot_release_private_bodies(self):
        for scope in ("channel", "requester"):
            result = await search_approved_knowledge(
                self.context, {"titles": ["Alliance policies"], "scope": scope},
            )
            self.assertEqual(result["sections"], [])
            self.assertTrue(result["available_privately"])
            self.assertEqual(self.context.state.required_access, set())
            self.assertNotIn("Alliance policies", str(result))

    async def test_exact_public_title_stays_public(self):
        result = await search_approved_knowledge(
            self.context, {"titles": ["Clan family lore"], "query": "Brown Elbow founding history"},
        )
        self.assertEqual([s["visibility"] for s in result["sections"]], ["public"])
        self.assertEqual(result["coverage"]["method"], "exact_titles")
        self.assertFalse(self.context.state.scope_audit.offered)
        self.assertEqual(await self.deliver(), "Synthetic answer")
        self.assertIn("Open matching titles first;", knowledge_tools()[0].definition.description)

    async def test_unauthorized_requester_gets_no_private_titles_or_hints(self):
        self.members[0] = Member(2, (Role(AGENT_TESTER_ROLE_ID),))
        for scope in ("channel", "requester"):
            result = await search_approved_knowledge(self.context, {"scope": scope})
            self.assertEqual([s["visibility"] for s in result["sections"]], ["public"])
            hidden = await search_approved_knowledge(
                self.context, {"titles": ["Alliance policies"], "scope": scope},
            )
            unknown = await search_approved_knowledge(
                self.context, {"titles": ["Unknown"], "scope": scope},
            )
            self.assertEqual(hidden, unknown)
        self.assertFalse(self.context.state.scope_audit.offered)
        self.assertFalse(self.context.state.knowledge_title_access)

    async def test_channel_readable_restricted_bodies_keep_paging(self):
        self.context = replace(self.context, delivery_channel=Channel(11, self.guild, {2, 99}))
        first = await search_approved_knowledge(self.context, {"titles": ["Alliance policies"]})
        section, = first["sections"]
        rest = await search_approved_knowledge(self.context, {
            "section_id": section["section_id"], "section_offset": section["next_section_offset"],
        })
        self.assertEqual(section["body"] + rest["sections"][0]["body"], self.catalog.active_sections[1].body)
        self.assertEqual(self.context.state.required_access, {"lead"})
        self.assertFalse(self.context.state.scope_audit.offered)

    async def test_private_thread_checks_unjoined_moderators_and_unknown_members(self):
        destination = Thread(11, self.channel, private=True, members=[self.requester])
        self.context = replace(self.context, delivery_channel=destination)
        with patch("elbow_helper.agent.disclosure.discord.Thread", Thread):
            result = await search_approved_knowledge(self.context, {})
            self.assertIn("lead", {s.get("visibility") for s in result["sections"]})
            # Knowledge checks access before and after loading; each recheck
            # must resolve current private-thread members and moderators.
            self.assertEqual(destination.fetch_members.await_count, 2)
            self.channel.moderators.add(self.reader.id)
            self.context = replace(self.context, state=AgentTurnState())
            self.context.disclosure_thread_members.clear()
            result = await search_approved_knowledge(self.context, {})
            self.assertEqual([s["visibility"] for s in result["sections"] if "body" in s], ["public"])
            self.channel.moderators.clear()
            self.context.disclosure_thread_members.clear()
            destination.fetch_members.return_value = [SimpleNamespace(id=404)]
            self.context = replace(self.context, state=AgentTurnState())
            self.context.disclosure_thread_members.clear()
            result = await search_approved_knowledge(self.context, {})
            self.assertEqual([s["visibility"] for s in result["sections"] if "body" in s], ["public"])

    async def test_invalid_scope_cannot_widen_search(self):
        result = await search_approved_knowledge(self.context, {"scope": "all"})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.required_access, set())
