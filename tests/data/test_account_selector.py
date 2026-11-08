"""Family polling and source discovery share the same stored-account union."""

import sqlite3
import unittest

from elbow_helper.data.store.players import due_accounts
from elbow_helper.data.store.sources import family_accounts


class FamilyAccountSelectorTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE accounts(id INTEGER, player_tag TEXT);
            CREATE TABLE clans(id INTEGER, is_family INTEGER);
            CREATE TABLE clan_membership(account_id INTEGER, clan_id INTEGER, to_at INTEGER);
            CREATE TABLE account_links(account_id INTEGER, to_at INTEGER);
            CREATE TABLE player_collection_state(account_id INTEGER, next_due_at INTEGER);
            INSERT INTO clans VALUES (1, 1), (2, 0);
            INSERT INTO accounts VALUES (7, '#7'), (6, '#6'), (5, '#5'), (4, '#4'), (3, '#3'), (2, '#2'), (1, '#1');
            INSERT INTO clan_membership VALUES (1, 1, NULL), (2, 2, NULL), (3, 1, 50), (4, 1, NULL), (6, 1, NULL), (7, 1, NULL);
            INSERT INTO account_links VALUES (2, NULL), (3, 50), (4, NULL), (5, 50);
            INSERT INTO player_collection_state VALUES (2, 100), (4, 101), (6, NULL), (7, 99);
        """)

    def test_union_is_current_deduplicated_and_ordered_and_due_filter_stays_local(self):
        self.assertEqual([dict(row) for row in family_accounts(self.connection)], [
            {"id": i, "player_tag": f"#{i}"} for i in (1, 2, 4, 6, 7)])
        self.assertEqual(due_accounts(self.connection, 100), [
            {"id": i, "player_tag": f"#{i}"} for i in (1, 2, 6, 7)])

    def test_sql_failures_still_propagate(self):
        self.connection.execute("DROP TABLE accounts")
        for reader in (family_accounts, lambda connection: due_accounts(connection, 100)):
            with self.assertRaises(sqlite3.OperationalError):
                reader(self.connection)
