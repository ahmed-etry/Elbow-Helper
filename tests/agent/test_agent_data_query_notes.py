"""Custom-count queries: table notes, member joins and the family discovery path."""
from contextlib import closing
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from agent.dataset_helpers import synthetic_datasets
from elbow_helper.agent.datasets.catalogue import SOURCES
from elbow_helper.agent.datasets.guide import DataGuide
from elbow_helper.agent.datasets.query import run_query
from elbow_helper.agent.access import KNOWN_ACCESS_REQUIREMENTS
from elbow_helper.agent.families.catalogue import FAMILY_INSTRUCTION

HEALTH_TABLES = (
    "wars", "war_attacks", "war_activity", "war_roster_members", "player_snapshots",
    "raid_member_activity", "player_directory",
)


def _notes():
    notes = {}
    for source in SOURCES:
        for table, _, note in source.tables:
            notes[source.alias + "." + table] = note
    return notes


class TableNoteTests(unittest.TestCase):
    def test_every_table_custom_counts_need_has_a_note_with_its_time_column(self):
        notes = _notes()
        for table, column in (
            ("wars", "end_ts"), ("war_attacks", "end_ts"), ("war_activity", "end_ts"),
            ("raid_member_activity", "end_ts"), ("player_snapshots", "captured_ts"),
            ("war_roster_members", "captured_ts"), ("player_directory", "first_seen_ts"),
        ):
            with self.subTest(table=table):
                note = notes["health." + table]
                self.assertIn(column, note)
                self.assertIn("UTC seconds", note)
        self.assertIn("discord_user_id", notes["links.links"])

    def test_notes_name_the_traps(self):
        notes = _notes()
        self.assertIn("not any one item's level", notes["health.player_snapshots"])
        self.assertIn("lifetime", notes["health.player_snapshots"])
        wars = notes["health.wars"]
        self.assertIn("friendly", wars)
        self.assertIn("82800", wars)
        self.assertIn("86400", wars)
        attacks = notes["health.war_attacks"]
        self.assertIn("defender_townhall", attacks)
        self.assertIn("attacker's town hall", attacks)
        self.assertIn("LEFT JOIN", attacks)

    def test_notes_stay_short(self):
        for name, note in _notes().items():
            if name.split(".")[0] in ("health", "links"):
                with self.subTest(table=name):
                    self.assertLessEqual(len(note), 700)

    def test_guide_lists_every_table_with_its_note(self):
        with TemporaryDirectory() as directory:
            guide = DataGuide(synthetic_datasets(directory)).for_levels(KNOWN_ACCESS_REQUIREMENTS)
        for table in HEALTH_TABLES:
            self.assertIn("health." + table + " [none]: ", guide)
        self.assertIn("links.links [none]: ", guide)

    def test_family_instruction_says_when_to_open_the_data_query(self):
        self.assertIn("custom counts", FAMILY_INSTRUCTION)
        self.assertIn("open group records with intent inspect", FAMILY_INSTRUCTION)
        self.assertIn("links.links", FAMILY_INSTRUCTION)
        self.assertIn("Family reads stay first", FAMILY_INSTRUCTION)


class MemberJoinTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.paths = synthetic_datasets(directory.name)
        sources = {source.alias: source for source in SOURCES}
        with closing(sqlite3.connect(sources["health"].resolve(self.paths))) as connection:
            connection.executemany(
                "INSERT INTO player_snapshots (captured_ts, clan_code, player_tag, player_name, townhall, "
                "hero_sum) VALUES (?, 'AA1', ?, ?, 10, ?)",
                [(100, "#AAA", "Synthetic One", 20), (200, "#AAA", "Synthetic One", 25),
                 (100, "#BBB", "Synthetic Two", 30), (200, "#BBB", "Synthetic Two", 30),
                 (100, "#CCC", "Synthetic Three", 5), (200, "#CCC", "Synthetic Three", 9)],
            )
            connection.commit()
        with closing(sqlite3.connect(sources["links"].resolve(self.paths))) as connection:
            connection.executemany(
                "INSERT INTO links (player_tag, discord_user_id) VALUES (?, ?)",
                [("#AAA", 11), ("#BBB", 11), ("#CCC", 22)],
            )
            connection.execute("INSERT INTO links (player_tag, discord_user_id) VALUES ('#DDD', 33)")
            connection.commit()

    def query(self, sql, params=None, max_rows=1000):
        return run_query(SOURCES, self.paths, set(), sql, params, max_rows)

    def test_account_tags_join_to_their_discord_member(self):
        result, touched = self.query(
            "SELECT k.discord_user_id AS member_id, COUNT(*) AS accounts, "
            "SUM(n.hero_sum - o.hero_sum) AS levels_gained "
            "FROM health.player_snapshots n "
            "JOIN health.player_snapshots o ON o.player_tag = n.player_tag AND o.captured_ts = :from_ts "
            "LEFT JOIN links.links k ON k.player_tag = n.player_tag "
            "WHERE n.captured_ts = :to_ts GROUP BY k.discord_user_id ORDER BY k.discord_user_id",
            {"from_ts": 100, "to_ts": 200},
        )
        self.assertNotIn("error", result)
        self.assertEqual(result["rows"], [
            {"member_id": 11, "accounts": 2, "levels_gained": 5},
            {"member_id": 22, "accounts": 1, "levels_gained": 4},
        ])
        self.assertFalse(touched)

    def test_unlinked_accounts_keep_their_row_with_no_member(self):
        result, _ = self.query(
            "SELECT s.player_tag, k.discord_user_id FROM health.player_snapshots s "
            "LEFT JOIN links.links k ON k.player_tag = s.player_tag "
            "WHERE s.captured_ts = 200 AND s.player_tag = '#ZZZ'",
        )
        self.assertEqual(result["rows"], [])
        with closing(sqlite3.connect(
                {s.alias: s for s in SOURCES}["health"].resolve(self.paths))) as connection:
            connection.execute(
                "INSERT INTO player_snapshots (captured_ts, clan_code, player_tag, player_name) "
                "VALUES (200, 'AA1', '#ZZZ', 'Synthetic Four')")
            connection.commit()
        result, _ = self.query(
            "SELECT s.player_tag, k.discord_user_id FROM health.player_snapshots s "
            "LEFT JOIN links.links k ON k.player_tag = s.player_tag "
            "WHERE s.captured_ts = 200 AND s.player_tag = '#ZZZ'",
        )
        self.assertEqual(result["rows"], [{"player_tag": "#ZZZ", "discord_user_id": None}])

    def test_oversized_per_account_result_reports_the_cut(self):
        result, _ = self.query(
            "SELECT player_tag, captured_ts FROM health.player_snapshots ORDER BY captured_ts, player_tag",
            max_rows=4,
        )
        self.assertEqual((result["row_count"], result["total_rows"], result["rows_cut"]), (4, 6, 2))
        self.assertTrue(result["truncated"])
        self.assertIn("incomplete", result["instruction"])


if __name__ == "__main__":
    unittest.main()
