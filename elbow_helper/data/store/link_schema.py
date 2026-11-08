"""Account ownership schema and current-link presentation metadata."""


def create_link_schema(connection):
    connection.execute("""CREATE TABLE imports (
        topic TEXT PRIMARY KEY, source_version INTEGER NOT NULL,
        source_digest TEXT NOT NULL, frozen_at INTEGER NOT NULL,
        current_links_digest TEXT NOT NULL, checked_at INTEGER NOT NULL,
        checks_json TEXT NOT NULL, source_item_id INTEGER NOT NULL REFERENCES collection_items(id))""")
    connection.execute("""CREATE TABLE link_imports (
        id INTEGER PRIMARY KEY, source_version INTEGER NOT NULL, source_digest TEXT NOT NULL,
        frozen_at INTEGER NOT NULL, basis TEXT NOT NULL, checks_json TEXT NOT NULL,
        source_item_id INTEGER NOT NULL REFERENCES collection_items(id))""")
    # Legacy names have unknown observation dates. Keep them searchable without
    # closing a fresher profile alias or claiming the import observed the name.
    connection.execute("""CREATE TABLE legacy_link_aliases (
        account_id INTEGER NOT NULL REFERENCES accounts(id), imported_at INTEGER NOT NULL,
        name TEXT NOT NULL, search_key TEXT NOT NULL, observed_at INTEGER,
        basis TEXT NOT NULL, source_item_id INTEGER NOT NULL REFERENCES collection_items(id),
        PRIMARY KEY(account_id,imported_at))""")
    connection.execute("CREATE INDEX legacy_link_alias_search ON legacy_link_aliases(search_key,account_id)")
    connection.execute("""CREATE TABLE link_operations (
        id INTEGER PRIMARY KEY, operation_key TEXT NOT NULL UNIQUE, requested_json TEXT NOT NULL,
        before_json TEXT NOT NULL, after_json TEXT NOT NULL,
        before_digest TEXT NOT NULL, after_digest TEXT NOT NULL,
        created_at INTEGER NOT NULL, finished_at INTEGER,
        status TEXT NOT NULL CHECK(status IN ('pending','complete','aborted')),
        source_item_id INTEGER REFERENCES collection_items(id))""")
    connection.execute("CREATE INDEX link_operations_pending ON link_operations(status,id)")
    connection.execute("CREATE UNIQUE INDEX link_one_pending ON link_operations((1)) WHERE status='pending'")
    connection.execute("""CREATE VIEW legacy_links AS
        SELECT a.player_tag,m.discord_id AS discord_user_id,d.player_name_last_seen,
               d.last_seen_clan_tag,d.last_seen_clan_code,d.last_seen_role
        FROM account_links l JOIN accounts a ON a.id=l.account_id
        JOIN members m ON m.id=l.member_id
        JOIN account_link_metadata d ON d.account_id=a.id WHERE l.to_at IS NULL""")


def create_current_link_schema(connection):
    # Presentation metadata preserves legacy spelling and natural row order;
    # account identity and dated ownership remain in accounts/account_links.
    connection.execute("ALTER TABLE account_link_metadata ADD COLUMN display_tag TEXT")
    connection.execute("ALTER TABLE account_link_metadata ADD COLUMN display_order INTEGER")
    # The old links file returned its rows in insertion order, which on live data
    # is tag order. Links already in the record carry no other trace of that order.
    # Ownership copied later takes its order from the old file in display_rows.
    connection.executemany("UPDATE account_link_metadata SET display_order=? WHERE account_id=?", [
        (position, row[0]) for position, row in enumerate(connection.execute(
            "SELECT d.account_id FROM account_link_metadata d JOIN accounts a ON a.id=d.account_id "
            "ORDER BY a.player_tag"), 1)])
    connection.execute("""CREATE VIEW current_link_rows AS
        SELECT COALESCE(d.display_tag,a.player_tag) AS player_tag,m.discord_id AS discord_user_id,
               d.player_name_last_seen,d.last_seen_clan_tag,d.last_seen_clan_code,d.last_seen_role
        FROM account_links l JOIN accounts a ON a.id=l.account_id
        JOIN members m ON m.id=l.member_id
        JOIN account_link_metadata d ON d.account_id=a.id WHERE l.to_at IS NULL
        ORDER BY d.display_order,a.id""")
