"""Indexed files must search visible sections rather than masquerade as titles."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.knowledge.store import KnowledgeCatalog, KnowledgeSection
from elbow_helper.agent.knowledge.tools import search_approved_knowledge
from tests.agent.engine.test_agent_plan_flow import _context


class CommunityRetrievalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sections = (
            KnowledgeSection("Competitive entry", "Cedar requires TH14+ and competitive war improvement.",
                             file_name="clan-profiles.md"),
            KnowledgeSection("Casual entry", "Birch is a relaxed clan.", file_name="clan-profiles.md"),
            KnowledgeSection("Family lore", "Pulling a moss means forgetting a signed-up war attack.",
                             file_name="culture.md"),
            KnowledgeSection("Other reference", "Cedar moss " * 100, file_name="other.md"),
            KnowledgeSection("Restricted notes", "Cedar recruitment decisions", "recruiter", "private.md"),
        )
        self.catalog = KnowledgeCatalog("synthetic", self.sections)
        self.context = replace(_context(), knowledge_store=SimpleNamespace(load=lambda: self.catalog))

    async def search(self, args):
        with (patch("elbow_helper.agent.knowledge.tools.require_evidence_access", AsyncMock()),
              patch("elbow_helper.agent.knowledge.tools.request_scope", AsyncMock(
                  return_value=SimpleNamespace(levels=frozenset(), requester=False)))):
            return await search_approved_knowledge(self.context, args)


    async def test_heading_opens_even_when_search_terms_do_not_match(self):
        result = await self.search({"titles": ["Family lore"], "query": "absent"})
        self.assertEqual(result["sections"][0]["body"], self.sections[2].body)
        self.assertEqual(result["coverage"]["method"], "exact_titles")

    async def test_file_browse_pages_sections_without_widening(self):
        first = await self.search({"titles": ["clan-profiles.md"], "limit": 1})
        last = await self.search({"titles": ["clan-profiles.md"], "limit": 1, "offset": first["next_offset"]})
        self.assertEqual(first["coverage"]["matching_sections"], 2)
        self.assertIsNone(last["next_offset"])
        self.assertEqual(last["sections"][0]["title"], "Casual entry")

    async def test_unknown_file_never_falls_through_to_other_sources(self):
        result = await self.search({"titles": ["missing.md"], "query": "Cedar"})
        self.assertEqual(result["sections"], [])

    async def test_public_file_selection_never_opens_restricted_file(self):
        result = await self.search({"titles": ["private.md"], "query": "Cedar"})
        self.assertEqual(result["sections"], [])
        self.assertNotIn("recruitment decisions", str(result))
