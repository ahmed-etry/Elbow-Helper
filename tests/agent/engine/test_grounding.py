"""Source integrity and useful recovery; semantic quality is evaluated with a real model."""

import unittest

from elbow_helper.agent.discord_actions.source_references import (
    GROUNDING_GAPS, GroundingError, ground_answer, partial_answer,
)


MESSAGE = {"message_id": 101, "author_id": 202, "author": "Member Alpha",
           "content": "I am not very social, but I plan to stay.",
           "source": "https://discord.com/channels/3/91/101"}


class GroundingTests(unittest.TestCase):
    def test_numeric_citations_render_linked_names_before_source_quotation(self):
        for citation in ('[[101]]', '[101]'):
            with self.subTest(citation=citation):
                answer = ground_answer('See ' + citation, {'read': {'messages': [MESSAGE]}})
                self.assertEqual(answer, f"See [Member Alpha]({MESSAGE['source']})")
        # Exact source quotations are escaped, not scanned for citation syntax.
        source = {**MESSAGE, 'content': 'A member wrote [[999]] as ordinary text.'}
        self.assertIn('999', ground_answer('[[message:101]]', [source]))

    def test_unknown_numeric_citation_uses_existing_repair_and_partial_recovery(self):
        with self.assertRaises(GroundingError):
            ground_answer('See [[999]].', [MESSAGE])
        recovered = partial_answer('Keep this advice. [[999]] [[101]]', [MESSAGE], GroundingError('unknown'))
        self.assertIn('Keep this advice.', recovered)
        self.assertIn(MESSAGE['source'], recovered)
        self.assertNotIn('[[', recovered)
        self.assertNotIn('999', recovered)


    def test_binding_renders_actual_author_and_words_with_source(self):
        answer = ground_answer("[[message:101]]\nMy recommendation: leave feedback optional.",
                               {"read": {"messages": [MESSAGE]}})
        self.assertIn("Member Alpha", answer)
        self.assertIn(MESSAGE["source"], answer)
        self.assertIn("> " + MESSAGE["content"], answer)
        self.assertIn("My recommendation: leave feedback optional.", answer)


    def test_unknown_failed_and_excerpted_sources_do_not_qualify(self):
        for evidence in (
            {}, {"read": {"error": "denied", "messages": [MESSAGE]}},
            {"read": {"error": "", "messages": [MESSAGE]}},
            {"read": {"flags": {"status": "refused"}, "messages": [MESSAGE]}},
            {"read": {"excerpt": str(MESSAGE)}},
            {"read": {"messages": [{**MESSAGE, "source": "https://example.invalid/101"}]}},
            {"read": {"messages": [{**MESSAGE, "source": "https://discord.com/channels/3/91/999"}]}},
        ):
            with self.subTest(evidence=evidence), self.assertRaises(GroundingError):
                ground_answer("[[message:101]]", evidence)


    def test_source_text_is_quoted_and_mentions_escaped(self):
        source = {**MESSAGE, "content": "First line\n@everyone **second line**"}
        answer = ground_answer("[[message:101]]", [source])
        self.assertNotIn("@everyone", answer)
        self.assertIn("\n> First line\n> ", answer)

    def test_partial_answer_keeps_prose_and_valid_referenced_sources(self):
        answer = partial_answer(
            "Do not preserve this unverified prose. [[message:101]] [[message:999]] " + MESSAGE["source"],
            {"read": {"messages": [MESSAGE]}}, GroundingError("unknown"),
        )
        self.assertIn(MESSAGE["content"], answer)
        self.assertIn(MESSAGE["source"], answer)
        self.assertNotIn(GROUNDING_GAPS["source"], answer)
        self.assertIn("unverified prose", answer)
        self.assertNotIn("999", answer)
