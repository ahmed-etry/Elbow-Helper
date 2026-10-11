"""Synthetic capability guidance renders whole or is rejected, never silently cut."""

import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.features.agent.plan.format import capability_list
from elbow_helper.infrastructure.ai import AgentToolDefinition


def registry(summary, note=""):
    definition = AgentToolDefinition(
        "synthetic_read", summary, {"type": "object", "properties": {}}, usage_note=note,
    )
    return {definition.name: RegisteredAgentTool(definition, AsyncMock())}


class CatalogueGuidanceTests(unittest.TestCase):
    def test_whole_summary_including_sentences_and_maximum_note_renders(self):
        summary = "Read synthetic records. Keep their exact values."
        note = "Use " + "a" * 155 + "."
        self.assertEqual(len(note), 160)
        line = capability_list(registry(summary, note))
        self.assertTrue(line.startswith("synthetic_read: " + summary + " | "))
        self.assertIn(" | use " + note, line)
        self.assertEqual(len(line.splitlines()), 1)

    def test_summary_at_limit_is_not_cut(self):
        summary = "Read " + "a" * 94 + "."
        self.assertEqual(len(summary), 100)
        self.assertIn(summary + " | ", capability_list(registry(summary)))

    def test_bad_guidance_is_rejected_instead_of_truncated(self):
        for summary, note in (
            ("a" * 101, ""), ("Read.\nMore.", ""), ("", ""),
            ("Read.", "a" * 161), ("Read.", "Use.\nMore."),
        ):
            with self.subTest(summary=summary, note=note), self.assertRaises(ValueError):
                capability_list(registry(summary, note))

    def test_note_is_optional(self):
        self.assertNotIn(" | use ", capability_list(registry("Read synthetic records.")))
