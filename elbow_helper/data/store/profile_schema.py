"""Old profile totals and source-specific history coverage, separate from live collecting."""

import sqlite3


def create_profile_history_schema(connection):
    # An account known only from an old file has never been seen by the collector.
    # Preserve every existing first-observed date; NULL lasts until a real observation.
    connection.execute("PRAGMA defer_foreign_keys=ON")
    # DROP invokes SET NULL even with deferred foreign keys. Restore those two
    # nullable references after the parent is rebuilt, inside this transaction.
    references = {table: connection.execute(
        f"SELECT rowid,account_id FROM {table} WHERE account_id IS NOT NULL").fetchall()
        for table in ("war_members", "cwl_players")}
    views = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='view'").fetchall()
    for name, _ in views:
        connection.execute('DROP VIEW "' + name.replace('"', '""') + '"')
    connection.execute("""CREATE TABLE accounts_with_history (
        id INTEGER PRIMARY KEY, player_tag TEXT NOT NULL UNIQUE, current_name TEXT,
        first_observed_at INTEGER, last_profile_at INTEGER, tracking_status TEXT NOT NULL)""")
    connection.execute("INSERT INTO accounts_with_history SELECT * FROM accounts")
    connection.execute("DROP TABLE accounts")
    connection.execute("ALTER TABLE accounts_with_history RENAME TO accounts")
    for table, rows in references.items():
        connection.executemany(f"UPDATE {table} SET account_id=? WHERE rowid=?", [(account, row) for row, account in rows])
    for _, statement in views:
        connection.execute(statement)
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise sqlite3.IntegrityError("profile_accounts_foreign_key_check")
    connection.execute("PRAGMA defer_foreign_keys=OFF")
    connection.execute("""CREATE TABLE old_profile_totals (
        account_id INTEGER NOT NULL REFERENCES accounts(id), observed_at INTEGER NOT NULL,
        hero_sum INTEGER, pet_sum INTEGER, equipment_sum INTEGER, troop_sum INTEGER,
        spell_sum INTEGER, games_total INTEGER, origin TEXT NOT NULL CHECK(origin='legacy_health'),
        source_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        PRIMARY KEY(account_id,observed_at))""")
    connection.execute("""CREATE TABLE profile_history_coverage (
        account_id INTEGER NOT NULL REFERENCES accounts(id), origin TEXT NOT NULL,
        first_attempt_at INTEGER NOT NULL, last_attempt_at INTEGER NOT NULL,
        first_good_at INTEGER, last_good_at INTEGER, samples INTEGER NOT NULL,
        partial_samples INTEGER NOT NULL, failed_samples INTEGER NOT NULL,
        gaps_json TEXT NOT NULL, failures_json TEXT NOT NULL, fields_json TEXT NOT NULL,
        source_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        PRIMARY KEY(account_id,origin))""")
