"""Family transactions for imported ownership and locally committed changes."""

from collections import Counter
import json

from .legacy_links import FIELDS, ImportMismatch
from .people import account, member, close_interval
from .projections import search_key


def compatibility(connection):
    rows = [dict(row) for row in connection.execute("SELECT * FROM legacy_links ORDER BY player_tag")]
    result = {row["player_tag"]: row for row in rows}
    if len(rows) != len(result):
        raise ImportMismatch("duplicate_active_ownership")
    return result


def mapping_counts(expected):
    checks = {"links": len(expected), "members": len({r["discord_user_id"] for r in expected.values()}),
              "owner_pairs": len(expected), "compatibility_fields": len(expected) * len(FIELDS)}
    checks["per_member_counts"] = {str(owner): count for owner, count in sorted(
        Counter(row["discord_user_id"] for row in expected.values()).items())}
    return checks


def check_mapping(connection, expected, *, constraints=False):
    actual = compatibility(connection)
    checks = mapping_counts(expected)
    if set(actual) != set(expected):
        raise ImportMismatch("normalized_tag_set_mismatch", checks)
    if {(tag, row["discord_user_id"]) for tag, row in actual.items()} != {
            (tag, row["discord_user_id"]) for tag, row in expected.items()}:
        raise ImportMismatch("owner_pairs_mismatch", checks)
    if Counter(row["discord_user_id"] for row in actual.values()) != Counter(
            row["discord_user_id"] for row in expected.values()):
        raise ImportMismatch("per_member_count_mismatch", checks)
    if actual != expected:
        raise ImportMismatch("compatibility_fields_mismatch", checks)
    active = connection.execute("SELECT COUNT(*) FROM account_links WHERE to_at IS NULL").fetchone()[0]
    if active != len(expected):
        raise ImportMismatch("active_link_count_mismatch", checks)
    if constraints:
        # Full-record scans belong to startup and recovery only.
        for table, key in (("accounts", "player_tag"), ("members", "discord_id")):
            if connection.execute(f"SELECT 1 FROM {table} GROUP BY {key} HAVING COUNT(*)>1").fetchone():
                raise ImportMismatch("duplicate_identity", checks)
        if connection.execute("SELECT 1 FROM account_links WHERE to_at IS NULL "
                              "GROUP BY account_id HAVING COUNT(*)>1").fetchone():
            raise ImportMismatch("duplicate_active_ownership", checks)
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ImportMismatch("family_foreign_key_check", checks)
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ImportMismatch("family_integrity_check", checks)
    return checks


def receipt(connection, kind, scope, at, count):
    run = connection.execute(
        "INSERT INTO collection_runs(collector,scope_kind,scheduled_at,started_at,finished_at,status,"
        "expected,attempted,succeeded,rows_written) VALUES (?,?,?,?,?,'ok',1,1,1,?)",
        (kind, scope, at, at, at, count),
    ).lastrowid
    return connection.execute(
        "INSERT INTO collection_items(run_id,ordinal,source_kind,scope_id,status,started_at,fetched_at,"
        "committed_at,returned_count,written_count,completeness_json) "
        "VALUES (?,0,?,?,'ok',?,?,?,?,?,'{\"complete\":true}')",
        (run, kind, scope, at, at, at, count, count),
    ).lastrowid


def apply_rows(connection, rows, at, item, *, basis, operation_key=None, reset=False, aliases=False):
    """Apply a canonical tag -> row/None patch without inventing clan stays."""
    query = "SELECT l.*,a.player_tag FROM account_links l JOIN accounts a ON a.id=l.account_id WHERE l.to_at IS NULL"
    current = {}
    if reset:
        current = {row["player_tag"]: row for row in connection.execute(query)}
    else:
        tags = tuple(rows)
        for start in range(0, len(tags), 500):
            batch = tags[start:start + 500]
            current.update((row["player_tag"], row) for row in connection.execute(
                query + f" AND a.player_tag IN ({','.join('?' for _ in batch)})", batch))
    selected = set(rows) | set(current) if reset else set(rows)
    for tag in sorted(selected):
        row = rows.get(tag)
        old = current.get(tag)
        account_id = old["account_id"] if old else account(connection, tag, at) if row else None
        owner = member(connection, row["discord_user_id"], at) if row else None
        changed = old is not None and owner != old["member_id"]
        if old and at < old["last_observed_at"]:
            raise ImportMismatch("freeze_precedes_observation")
        if changed:
            close_interval(connection, "account_links", old, at, item)
        if row and (old is None or changed):
            connection.execute(
                "INSERT INTO account_links(account_id,member_id,from_at,last_observed_at,basis,"
                "operation_id,source_item_id) VALUES (?,?,?,?,?,?,?)",
                (account_id, owner, at, at, basis, operation_key, item),
            )
        elif row:
            if reset:
                # Import confirms this owner; retain the original interval while
                # recording the authoritative import's evidence on the row.
                connection.execute("UPDATE account_links SET last_observed_at=?,basis=?,source_item_id=? WHERE id=?",
                                   (at, basis, item, old["id"]))
            else:
                connection.execute("UPDATE account_links SET last_observed_at=? WHERE id=?", (at, old["id"]))
        if row:
            connection.execute(
                "INSERT INTO account_link_metadata(account_id,player_name_last_seen,last_seen_clan_tag,"
                "last_seen_clan_code,last_seen_role,observed_at,basis,source_item_id) "
                "VALUES (?,?,?,?,?,NULL,'legacy_unknown_age',?) ON CONFLICT(account_id) DO UPDATE SET "
                "player_name_last_seen=excluded.player_name_last_seen,last_seen_clan_tag=excluded.last_seen_clan_tag,"
                "last_seen_clan_code=excluded.last_seen_clan_code,last_seen_role=excluded.last_seen_role,"
                "observed_at=NULL,basis=excluded.basis,source_item_id=excluded.source_item_id",
                (account_id, *(row[key] for key in FIELDS[2:]), item),
            )
            if aliases and row["player_name_last_seen"] is not None:
                name = row["player_name_last_seen"]
                connection.execute("INSERT INTO legacy_link_aliases VALUES (?,?,?,?,NULL,'legacy_unknown_age',?)",
                                   (account_id, at, name, search_key(name), item))


def requested_patch(legacy, requested):
    """Preserve existing raw legacy keys while resolving canonical selectors."""
    from elbow_helper.domain.player_tags import normalize_player_tag
    keys = {normalize_player_tag(tag): tag for tag in legacy}
    before, after = {}, {}
    for tag, value in requested.items():
        key = keys.get(tag, tag)
        before[key] = legacy.get(key)
        after[key] = {**value, "player_tag": key} if value is not None else None
    return before, after


def patched(rows, patch):
    result = dict(rows)
    for tag, row in patch.items():
        if row is None:
            result.pop(tag, None)
        else:
            result[tag] = row
    return result


def operation_patch(operation):
    from .projections import link_projection
    after = json.loads(operation["after_json"])
    # Deleted keys still need canonical identities for closing ownership.
    from elbow_helper.domain.player_tags import normalize_player_tag
    return {normalize_player_tag(tag): None for tag, row in after.items() if row is None} | {
        row["player_tag"]: row for row in link_projection({tag: row for tag, row in after.items() if row is not None})}
