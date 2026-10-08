"""All declared sources have schema-derived guides and verified config paths."""
import importlib
from tempfile import TemporaryDirectory
import unittest
from pathlib import Path
from features.agent.dataset_helpers import synthetic_datasets
from elbow_helper.features.agent.datasets.catalogue import SOURCES
from elbow_helper.features.agent.datasets.guide import DataGuide
from elbow_helper.features.agent.access import KNOWN_ACCESS_REQUIREMENTS
from elbow_helper.features.agent.conversation.context import estimate_tokens


class DataGuideTests(unittest.TestCase):
    def test_computed_owner_and_health_verdicts_are_identified_in_notes(self):
        with TemporaryDirectory() as directory:
            guide = DataGuide(synthetic_datasets(directory)).for_levels(KNOWN_ACCESS_REQUIREMENTS)
        self.assertIn("status, flags_json and note are clan health's stored verdicts", guide)
        self.assertIn("proposed_discord_user_id and proposed_display_name", guide)
        self.assertIn("account links' stored owner verdict, not verified ownership", guide)

    def test_every_table_and_state_has_a_note_and_feature_constant(self):
        for source in SOURCES:
            with self.subTest(alias=source.alias, state=source.state_name):
                if source.alias != "achievements":
                    module, name = source.config_constant.rsplit(".", 1)
                    self.assertEqual(
                        Path(getattr(importlib.import_module(module), name)), source.path,
                    )
                for table, level, note in source.tables:
                    self.assertTrue(note.strip())
                    self.assertIn(level, KNOWN_ACCESS_REQUIREMENTS | {"none"})
                if source.state_name:
                    self.assertTrue(source.note.strip())
        with TemporaryDirectory() as directory:
            paths = synthetic_datasets(directory)
            guide = DataGuide(paths).for_levels(KNOWN_ACCESS_REQUIREMENTS)
            import sqlite3
            from contextlib import closing
            for source in SOURCES:
                for table, _, _ in source.tables:
                    with closing(sqlite3.connect(source.resolve(paths))) as connection:
                        exists = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
                    if exists:
                        self.assertIn(source.alias + "." + table + " [", guide)
                    else:
                        self.assertNotIn(source.alias + "." + table + " [", guide)
                if source.state_name:
                    self.assertIn("state." + source.state_name + " [", guide)
            self.assertLessEqual(estimate_tokens(guide), 10_000)
            self.assertNotIn(
                "state.cwl_router", DataGuide(paths).for_levels(set()).split("Not available")[0],
            )
