"""Exact knowledge opens preserve search visibility and retained evidence checks."""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.configuration.roles import AGENT_TESTER_ROLE_ID, CORE, LEAD, LEAD_PLUS
from elbow_helper.features.agent.access import AgentAccessLost, LookupAccessDenied
from elbow_helper.features.agent.knowledge.store import KnowledgeStore
from elbow_helper.features.agent.knowledge.tools import knowledge_tools, search_approved_knowledge
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.plan.checker import valid_arguments


class KnowledgeToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.store = KnowledgeStore(self.path)
        self.member = SimpleNamespace(id=1, roles=[SimpleNamespace(id=AGENT_TESTER_ROLE_ID)])
        channel = SimpleNamespace(permissions_for=lambda _: SimpleNamespace(
            view_channel=True, read_message_history=True))
        guild = SimpleNamespace(me=self.member, get_member=lambda _: self.member)
        self.context = SimpleNamespace(
            knowledge_store=self.store, member=self.member, guild=guild,
            source_message=SimpleNamespace(channel=channel, created_at=datetime.now(timezone.utc)),
            state=AgentTurnState(),
        )

    def write(self, name, text):
        (self.path / name).write_text(text, encoding="utf-8")

    async def test_titles_open_only_exact_sections_in_requested_order(self):
        self.write("topics.md", "# Alpha guide\nAlpha body\n# Beta guide\nBeta body\n"
                   "# Other\nAlpha guide Beta guide")
        result = await search_approved_knowledge(
            self.context, {"titles": ["Beta guide", "Alpha guide"], "query": "Other"},
        )
        self.assertEqual(result["sections"], [
            {"title": "Beta guide", "body": "Beta body", "visibility": "public"},
            {"title": "Alpha guide", "body": "Alpha body", "visibility": "public"},
        ])
        for title in ("alpha guide", "Alpha", "Alpha guides", "Missing"):
            with self.subTest(title=title):
                self.assertEqual(await search_approved_knowledge(self.context, {"titles": [title]}),
                                 {"sections": []})

    async def test_hidden_titles_are_indistinguishable_from_unknown_titles(self):
        self.write("public.md", "# Shared title\nPublic body")
        for level in ("lead", "lead_plus", "core"):
            self.write(level + ".md", f'+++\nvisibility="{level}"\n+++\n'
                       f'# Hidden {level}\nRestricted body\n# Shared title\nRestricted duplicate')
        for level in ("lead", "lead_plus", "core"):
            hidden = await search_approved_knowledge(self.context, {"titles": [f"Hidden {level}"]})
            unknown = await search_approved_knowledge(self.context, {"titles": ["Unknown"]})
            self.assertEqual(hidden, unknown)
        result = await search_approved_knowledge(self.context, {"titles": ["Shared title"]})
        self.assertEqual(result["sections"], [
            {"title": "Shared title", "body": "Public body", "visibility": "public"},
        ])
        self.assertEqual(self.context.state.required_access, set())

    async def test_titles_retain_each_visibility_and_recheck_access(self):
        roles = {"lead": LEAD, "lead_plus": LEAD_PLUS, "core": CORE}
        for level, role_ids in roles.items():
            with self.subTest(level=level):
                self.write(level + ".md", f'+++\nvisibility="{level}"\n+++\n# {level} topic\nBody')
                self.store = KnowledgeStore(self.path)
                self.context.knowledge_store = self.store
                self.context.state = AgentTurnState()
                self.member.roles = [SimpleNamespace(id=AGENT_TESTER_ROLE_ID),
                                     SimpleNamespace(id=next(iter(role_ids)))]
                result = await search_approved_knowledge(self.context, {"titles": [f"{level} topic"]})
                self.assertEqual(result["sections"], [
                    {"title": f"{level} topic", "body": "Body", "visibility": level},
                ])
                self.assertEqual(self.context.state.required_access, {level})
                self.member.roles.pop()
                with self.assertRaises(AgentAccessLost):
                    await search_approved_knowledge(self.context, {"titles": ["Unknown"]})

    async def test_access_loss_during_title_selection_is_rechecked(self):
        self.write("private.md", '+++\nvisibility="lead"\n+++\n# Restricted\nBody')
        self.member.roles.append(SimpleNamespace(id=next(iter(LEAD))))
        catalog = self.store.load()
        visible_sections = type(catalog).visible_sections

        def lose_access(catalog, levels):
            visible = visible_sections(catalog, levels)
            self.member.roles.pop()
            return visible

        with patch.object(type(catalog), "visible_sections", lose_access):
            with self.assertRaises(LookupAccessDenied):
                await search_approved_knowledge(self.context, {"titles": ["Restricted"]})
        self.assertEqual(self.context.state.required_access, set())

    async def test_title_body_bounds_and_query_compatibility(self):
        self.write("topics.md", "# Large\n" + "x" * 5000 + "\n# Searchable\nGeneric body")
        opened = await search_approved_knowledge(self.context, {"titles": ["Large"]})
        self.assertEqual(len(opened["sections"][0]["body"]), 4000)
        searched = await search_approved_knowledge(self.context, {"query": "searchable"})
        self.assertEqual([section["title"] for section in searched["sections"]], ["Searchable"])
        self.assertEqual(await search_approved_knowledge(self.context, {}), {"sections": []})
        self.context.knowledge_store = None
        self.assertEqual(await search_approved_knowledge(self.context, {"titles": ["Large"]}),
                         {"sections": []})

    async def test_conversation_access_is_rechecked_after_opening_titles(self):
        self.write("public.md", "# Public topic\nBody")
        catalog = self.store.load()
        visible_sections = type(catalog).visible_sections

        def lose_access(catalog, levels):
            visible = visible_sections(catalog, levels)
            self.context.source_message.channel.permissions_for = lambda _: SimpleNamespace(
                view_channel=False, read_message_history=False)
            return visible

        with patch.object(type(catalog), "visible_sections", lose_access):
            with self.assertRaises(AgentAccessLost):
                await search_approved_knowledge(self.context, {"titles": ["Public topic"]})

    def test_tool_schema_accepts_either_argument_or_both_and_bounds_titles(self):
        schema = knowledge_tools()[0].definition.parameters
        for arguments in ({"query": "topic"}, {"query": ""}, {"titles": ["Topic"]},
                          {"query": "topic", "titles": ["Topic"]},
                          {"titles": [f"Topic {i}" for i in range(5)]}):
            with self.subTest(arguments=arguments):
                self.assertTrue(valid_arguments(arguments, schema))
        for arguments in ({"titles": []}, {"titles": ["Topic"] * 2},
                          {"titles": [f"Topic {i}" for i in range(6)]},
                          {"titles": "Topic"}, {"titles": [1]}, {"query": "x" * 201}):
            with self.subTest(arguments=arguments):
                self.assertFalse(valid_arguments(arguments, schema))
