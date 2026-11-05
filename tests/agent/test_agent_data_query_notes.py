"""Custom-count queries: table notes, member joins and the family discovery path."""
from contextlib import closing
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from agent.dataset_helpers import synthetic_datasets, seed_record_links
from elbow_helper.agent.datasets.catalogue import SOURCES
from elbow_helper.agent.datasets.guide import DataGuide
from elbow_helper.agent.datasets.query import run_query
from elbow_helper.agent.access import KNOWN_ACCESS_REQUIREMENTS
from elbow_helper.agent.families.catalogue import FAMILY_INSTRUCTION

# Clan health's own tables: its settings and their change log. Its activity facts are in the family record.
HEALTH_TABLES = ("clan_player_health_config", "clan_health_config_audit")


def _notes():
    notes = {}
    for source in SOURCES:
        for table, _, note in source.tables:
            notes[source.alias + "." + table] = note
    return notes


class TableNoteTests(unittest.TestCase):
    def test_every_table_custom_counts_need_has_a_note_with_its_time_column(self):
        notes = _notes()
        self.assertEqual(sorted(name for name in notes if name.startswith("health.")),
                         sorted("health." + table for table in HEALTH_TABLES))
        self.assertIn("ts_utc is ISO UTC", notes["health.clan_health_config_audit"])
        self.assertIn("discord_user_id", notes["links.links"])
        for table in ("wars", "war_sides", "war_members", "war_attacks"):
            self.assertIn("end_at", notes["family." + table])
            self.assertIn("UTC microseconds", notes["family." + table])

    def test_notes_name_the_traps(self):
        notes = _notes()
        wars = notes["family.wars"]
        self.assertIn("friendly", wars)
        self.assertIn("FRIENDLY", wars)
        attacks = notes["family.war_attacks"]
        self.assertIn("defender_townhall", attacks)
        self.assertIn("attacker's town hall", attacks)
        self.assertIn("LEFT JOIN", attacks)
        misses = notes['family.war_members']
        for proof in ("state='warEnded'", 'non-NULL', 'member count=team_size',
                      'non-stub', 'count=war_sides.attacks', 'derived', 'unattributed', 'unknown'):
            self.assertIn(proof, misses)

    def test_notes_stay_short(self):
        for name, note in _notes().items():
            if name.split(".")[0] in ("family", "health", "links"):
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
        with closing(sqlite3.connect(sources["family"].resolve(self.paths))) as connection:
            connection.execute("INSERT INTO clans(id,tag,family_code,name,is_family,wiring_revision) "
                               "VALUES (1,'#PP9','AA1','Synthetic clan',1,1)")
            for index, tag in enumerate(("#AAA", "#BBB", "#CCC", "#ZZZ", "#YYY", "#XXX"), start=100):
                connection.execute("INSERT INTO accounts(id,player_tag,first_observed_at,tracking_status) "
                                   "VALUES (?,?,1,'observed')", (index, tag))
                connection.execute("INSERT INTO clan_membership(account_id,clan_id,clan_role,from_at,last_observed_at,"
                                   "source_item_id) VALUES (?,1,'member',?,1,1)", (index, index))
            connection.commit()
        seed_record_links(self.paths, [("#AAA", 11), ("#BBB", 11), ("#CCC", 22), ("#DDD", 33)])

    def query(self, sql, params=None, max_rows=1000):
        return run_query(SOURCES, self.paths, set(), sql, params, max_rows)

    def test_account_tags_join_to_their_discord_member(self):
        result, touched = self.query(
            "SELECT k.discord_user_id AS member_id, COUNT(*) AS accounts "
            "FROM family.clan_membership m JOIN family.accounts a ON a.id = m.account_id "
            "JOIN links.links k ON k.player_tag = a.player_tag "
            "WHERE m.to_at IS NULL AND a.player_tag IN ('#AAA','#BBB','#CCC') "
            "GROUP BY k.discord_user_id ORDER BY k.discord_user_id",
        )
        self.assertNotIn("error", result)
        self.assertEqual(result["rows"], [{"member_id": 11, "accounts": 2}, {"member_id": 22, "accounts": 1}])
        self.assertFalse(touched)

    def test_unlinked_accounts_keep_their_row_with_no_member(self):
        result, _ = self.query(
            "SELECT a.player_tag, k.discord_user_id FROM family.accounts a "
            "LEFT JOIN links.links k ON k.player_tag = a.player_tag WHERE a.player_tag = '#ZZZ'",
        )
        self.assertEqual(result["rows"], [{"player_tag": "#ZZZ", "discord_user_id": None}])

    def test_oversized_per_account_result_reports_the_cut(self):
        result, _ = self.query("SELECT player_tag FROM family.accounts ORDER BY player_tag", max_rows=4)
        # Six clan members and the linked account the ownership seed adds.
        self.assertEqual((result["row_count"], result["total_rows"], result["rows_cut"]), (4, 7, 3))
        self.assertTrue(result["truncated"])
        self.assertIn("incomplete", result["instruction"])


if __name__ == "__main__":
    unittest.main()
