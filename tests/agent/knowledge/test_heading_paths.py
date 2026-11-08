"""Nested policy sections retain context through ranking, opening and paging."""

from pathlib import Path
from tests.temp_directory import TemporaryDirectory
import unittest

from elbow_helper.agent.conversation.context import estimate_tokens
from elbow_helper.agent.knowledge.store import (
    KnowledgeCatalog, KnowledgeStore, SECTION_TOKEN_TARGET,
)
from elbow_helper.agent.knowledge.tools import _section_id


class HeadingPathTests(unittest.TestCase):
    def catalog(self, text):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "rules.md"
        path.write_text(text, encoding="utf-8", newline="\n")
        return KnowledgeStore(path.parent).load()

    def test_policy_applicability_survives_section_splitting_without_being_inferred(self):
        scope = 'Starting a new program; existing programs use their current agreement.'
        body = 'Applies to: ' + scope + '\n\n' + '\n\n'.join(
            'A documented requirement. ' * 400 for _ in range(3))
        sections = self.catalog('# Program entry\n' + body).active_sections
        self.assertGreater(len(sections), 1)
        self.assertTrue(all(section.applicability == scope for section in sections))
        self.assertEqual('\n\n'.join(section.body for section in sections), body.strip())
        self.assertEqual(self.catalog('# An ordinary fact\nA fact.').active_sections[0].applicability, '')


    def test_large_sections_split_at_paragraphs_with_paths_and_unique_ids(self):
        paragraphs = [f"Paragraph {index}. " + "Activity needs evidence. " * 160 for index in range(5)]
        body = "\n\n".join(paragraphs).strip()
        catalog = self.catalog("# Rank rules\n## Co-Leader\n### Requirements\n" + body)
        sections = catalog.active_sections
        self.assertGreater(len(sections), 1)
        self.assertEqual("\n\n".join(section.body for section in sections), body)
        self.assertTrue(all(section.title == "Rank rules › Co-Leader › Requirements" for section in sections))
        self.assertTrue(all(estimate_tokens(section.title + "\n" + section.body) <= SECTION_TOKEN_TARGET
                            for section in sections))
        self.assertEqual([section.part for section in sections], list(range(1, len(sections) + 1)))
        self.assertTrue(all(section.parts == len(sections) for section in sections))
        self.assertEqual(len({_section_id(section) for section in sections}), len(sections))
        self.assertEqual(catalog.matching_titles(sections, ["requirements"]), sections)
        self.assertEqual(catalog.prompt().index.count("Rank rules › Co-Leader › Requirements"), 1)
        self.assertIn(f"({len(sections)} parts)", catalog.prompt().index)

    def test_fences_and_oversized_paragraphs_stay_whole(self):
        block = "````markdown\n# Quoted heading\n\n```\n## Still quoted\n````"
        oversized = "Long paragraph. " * 2000
        catalog = self.catalog("# Rules\n" + block + "\n\n" + oversized + "\n\nTail.")
        self.assertEqual([section.body for section in catalog.active_sections], [block, oversized, "Tail."])
        self.assertTrue(all(section.title == "Rules" for section in catalog.active_sections))

    def test_duplicate_parts_have_distinct_ids_and_leaf_ambiguity_uses_visible_paths(self):
        paragraph = "Identical paragraph. " * 350
        catalog = self.catalog("# Public\n## Requirements\n" + paragraph + "\n\n" + paragraph)
        sections = catalog.active_sections
        self.assertEqual(len(sections), 2)
        self.assertNotEqual(_section_id(sections[0]), _section_id(sections[1]))
        hidden = type(sections[0])("Private › Requirements", "Secret", "core", "private.md")
        combined = KnowledgeCatalog("", (*sections, hidden))
        self.assertEqual(combined.matching_titles(combined.visible_sections(), ["requirements"]), sections)
        self.assertEqual(combined.matching_titles(combined.visible_sections({"core"}), ["requirements"]), ())
