"""Custom-count queries: table notes, member joins and the family discovery path."""
from contextlib import closing
import sqlite3
from tests.temp_directory import TemporaryDirectory
import unittest

from tests.agent.reads.dataset_helpers import synthetic_datasets, seed_record_links
from elbow_helper.agent.reads.sql.catalogue import SOURCES
from elbow_helper.agent.reads.sql.query import run_query

# Clan health's own tables: its settings and their change log. Its activity facts are in the family record.
HEALTH_TABLES = ("clan_player_health_config", "clan_health_config_audit")


def _notes():
    notes = {}
    for source in SOURCES:
        for table, _, note in source.tables:
            notes[source.alias + "." + table] = note
    return notes


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


if __name__ == "__main__":
    unittest.main()
