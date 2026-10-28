"""Additive schema for checked legacy imports and ordered link mirroring."""


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
