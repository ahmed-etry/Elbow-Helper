"""Approved knowledge remains verbatim; editorial changes belong to the owner."""
from pathlib import Path
from tests.temp_directory import TemporaryDirectory
import unittest

from elbow_helper.agent.reads.domain_facts import clan_profile
from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.knowledge.store import KnowledgeStore


class WithdrawnPolicyTests(unittest.TestCase):

    def test_store_does_not_rewrite_a_section_by_file_or_heading(self):
        with TemporaryDirectory() as directory:
            Path(directory, 'clan-profiles.md').write_text(
                '# BEP / Plutopia / The Backseat: CWL, not an active home clan\n'
                'Plutopia is used for CWL and is not an active home clan.\n', encoding='utf-8')
            catalog = KnowledgeStore(Path(directory)).load()
            section, = catalog.active_sections
            self.assertEqual(section.title, 'BEP / Plutopia / The Backseat: CWL, not an active home clan')
            self.assertEqual(section.body, 'Plutopia is used for CWL and is not an active home clan.')
            self.assertIn(section.title, catalog.prompt().text)
            self.assertEqual(catalog.matching_titles((section,), [section.title]), (section,))
        profile = clan_profile('BEP')
        self.assertIsNone(profile['membership_basis'])
        self.assertIsNone(profile['operational_cwl_only'])
        self.assertEqual(profile['configured_membership_basis'],
                         'seasonal_cwl_cohort' if CLANS['BEP'].cwl_only else 'current_in_game')
