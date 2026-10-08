"""Markdown knowledge defaults, access, ranking and prompt bounds."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from elbow_helper.features.agent.knowledge.store import KnowledgeStore
from elbow_helper.features.agent.knowledge.tools import search_approved_knowledge
from elbow_helper.features.agent.models import AgentTurnState


class KnowledgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.store = KnowledgeStore(self.path)

    def write(self, name, text):
        (self.path / name).write_text(text, encoding="utf-8")

    def test_plain_front_matter_and_old_fields(self):
        self.write("plain.md", "Introduction\n# War\nHit your mirror.\n## Raids\nUse attacks.")
        self.write("old.md", """+++
key="old"
version=7
title="Old title"
topics=["war"]
source_refs=[]
effective_from="2099-01-01"
reviewed_at="2000-01-01"
approved_by=1
visibility="lead"
status="approved"
expires_at="2001-01-01"
+++
# Leads
Review wars.""")
        for status in ("draft", "retired"):
            self.write(status + ".md", f"""+++
status="{status}"
+++
Hidden""")
        sections = self.store.load().active_sections
        self.assertEqual([s.title for s in sections], ["Leads", "plain", "War", "Raids"])
        self.assertEqual(sections[0].visibility, "lead")
        self.assertEqual(sections[1].body, "Introduction")
        self.assertNotIn("Review wars", self.store.load().public_prompt())

    def test_rank_and_reload_interval(self):
        self.write("facts.md", "# War\nOther facts\n# Other\nwar war war")
        with patch("elbow_helper.features.agent.knowledge.store.time.monotonic", return_value=0):
            first = self.store.load()
        self.assertEqual(first.search(first.active_sections, query="war")[0].title, "War")
        self.write("facts.md", "# War\nNew facts")
        with patch("elbow_helper.features.agent.knowledge.store.time.monotonic", return_value=59):
            self.assertIs(self.store.load(), first)
        with patch("elbow_helper.features.agent.knowledge.store.time.monotonic", return_value=60):
            self.assertEqual(self.store.load().active_sections[0].body, "New facts")

    def test_prompt_threshold(self):
        self.write("large.md", "# Facts\n" + "x" * 20000)
        self.assertEqual(self.store.load().public_prompt(), "")
        with patch(
            "elbow_helper.features.agent.knowledge.store.time.monotonic", return_value=10**12,
        ):
            self.write("large.md", "# Facts\nA fact")
            self.assertIn("A fact", self.store.load().public_prompt())

    async def test_visibility_and_result_bounds(self):
        self.write("public.md", "".join(
            f"# Public {index}\n" + "x" * 5000 + "\n" for index in range(10)
        ))
        for level in ("lead", "lead_plus", "core"):
            self.write(level + ".md", f"""+++
visibility="{level}"
+++
# Secret {level}
Secret facts""")
        context = SimpleNamespace(
            knowledge_store=self.store, member=SimpleNamespace(id=1),
            guild=object(), state=AgentTurnState(),
        )
        with (
            patch(
                "elbow_helper.features.agent.knowledge.tools.require_evidence_access", AsyncMock(),
            ),
            patch(
                "elbow_helper.features.agent.knowledge.tools.has_access_requirements",
                return_value=False,
            ),
        ):
            result = await search_approved_knowledge(context, {"query": ""})
            self.assertEqual(len(result["sections"]), 8)
            self.assertTrue(all(
                len(s["body"]) <= 4000 and s["visibility"] == "public" for s in result["sections"]
            ))
            self.assertEqual(
                (await search_approved_knowledge(context, {"query": "secret"}))["sections"], [],
            )
        with (
            patch(
                "elbow_helper.features.agent.knowledge.tools.require_evidence_access", AsyncMock(),
            ),
            patch(
                "elbow_helper.features.agent.knowledge.tools.has_access_requirements",
                return_value=True,
            ),
            patch("elbow_helper.features.agent.knowledge.tools.require_lookup_access"),
        ):
            result = await search_approved_knowledge(context, {"query": "secret"})
            self.assertEqual(len(result["sections"]), 3)
            self.assertEqual(context.state.required_access, {"lead", "lead_plus", "core"})
