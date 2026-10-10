"""Chosen CWL wars are checked against synthetic stored metadata before scoring."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.cwl.queries import CwlQueries


class ChosenWarStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = ClanHealthRepository(Path(temporary.name) / "wars.sqlite3")
        self.repository.initialize()
        with closing(sqlite3.connect(self.repository.path)) as connection, connection:
            connection.executemany(
                "INSERT INTO wars (war_id, war_type, clan_code, clan_tag, last_seen_ts, "
                "state, cwl_season, cwl_round, cwl_league) VALUES (?, ?, ?, '', 1, ?, ?, 1, ?)",
                [
                    ("CWL:#DONE", "CWL", "BEH", "warEnded", "2026-08", "Champion League II"),
                    ("CWL:#ACTIVE", "CWL", "BEH", "inWar", "2026-08", "Champion League II"),
                    ("CWL:#OTHER", "CWL", "BE1", "warEnded", "2026-08", "Champion League II"),
                    ("REGULAR", "Random", "BEH", "warEnded", "", ""),
                ],
            )
        self.queries = CwlQueries(self.repository, lambda: {})

    def test_unknown_and_unfinished_wars_are_named_together(self):
        with self.assertRaisesRegex(ValueError, "Unknown CWL war IDs: #UNKNOWN.*#ACTIVE"):
            self.queries.ass_wars(clan_code="BEH", war_ids=["#UNKNOWN", "CWL:#ACTIVE"])

    def test_other_clan_and_regular_wars_are_refused(self):
        for key, error in (("#OTHER", "another clan than BEH: CWL:#OTHER"),
                           ("REGULAR", "Not CWL wars: REGULAR")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, error):
                self.queries.ass_wars(clan_code="BEH", war_ids=[key])

    def test_only_the_chosen_finished_war_is_loaded(self):
        wars = self.repository.scoring_wars("BEH", ["#DONE"])
        self.assertEqual([war["war_id"] for war in wars], ["CWL:#DONE"])
        self.assertEqual(wars[0]["state"], "warEnded")
        self.assertEqual(wars[0]["roster"], [])
        self.assertEqual(wars[0]["attacks"], [])

    def test_same_war_stored_for_both_clans_uses_the_requested_clan(self):
        with closing(sqlite3.connect(self.repository.path)) as connection, connection:
            connection.execute(
                "INSERT INTO wars (war_id, war_type, clan_code, clan_tag, last_seen_ts, "
                "state, cwl_season) VALUES ('CWL:#DONE', 'CWL', 'BE1', '', 1, "
                "'warEnded', '2026-08')",
            )
        wars = self.repository.scoring_wars("BEH", ["#DONE"])
        self.assertEqual([war["clan_code"] for war in wars], ["BEH"])
