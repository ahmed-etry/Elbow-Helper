"""Atomic ownership changes and current-link presentation in the family record."""

from hashlib import sha256

from .people import account, member, close_interval
from .projections import canonical_json


FIELDS = ("player_tag", "discord_user_id", "player_name_last_seen", "last_seen_clan_tag",
          "last_seen_clan_code", "last_seen_role")
TOPIC = "people_membership"


class LinkConflict(RuntimeError):
    """Ownership cannot safely be read or changed."""


def digest(value):
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def compatibility(connection):
    rows = [dict(row) for row in connection.execute("SELECT * FROM legacy_links ORDER BY player_tag")]
    result = {row["player_tag"]: row for row in rows}
    if len(rows) != len(result):
        raise LinkConflict("duplicate_active_ownership")
    return result


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


def apply_rows(connection, rows, at, item, *, basis, operation_key=None):
    """Apply a canonical tag -> row/None patch without inventing clan stays."""
    query = "SELECT l.*,a.player_tag FROM account_links l JOIN accounts a ON a.id=l.account_id WHERE l.to_at IS NULL"
    current = {}
    tags = tuple(rows)
    for start in range(0, len(tags), 500):
        batch = tags[start:start + 500]
        current.update((row["player_tag"], row) for row in connection.execute(
            query + f" AND a.player_tag IN ({','.join('?' for _ in batch)})", batch))
    selected = set(rows)
    for tag in sorted(selected):
        row = rows.get(tag)
        old = current.get(tag)
        account_id = old["account_id"] if old else account(connection, tag, at) if row else None
        owner = member(connection, row["discord_user_id"], at) if row else None
        changed = old is not None and owner != old["member_id"]
        if old and at < old["last_observed_at"]:
            raise LinkConflict("freeze_precedes_observation")
        if changed:
            close_interval(connection, "account_links", old, at, item)
        if row and (old is None or changed):
            connection.execute(
                "INSERT INTO account_links(account_id,member_id,from_at,last_observed_at,basis,"
                "operation_id,source_item_id) VALUES (?,?,?,?,?,?,?)",
                (account_id, owner, at, at, basis, operation_key, item),
            )
        elif row:
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


def patched(rows, patch):
    result = dict(rows)
    for tag, row in patch.items():
        if row is None:
            result.pop(tag, None)
        else:
            result[tag] = row
    return result


def display_rows(connection, rows, *, reorder=False):
    next_order = connection.execute("SELECT COALESCE(MAX(display_order),0) FROM account_link_metadata").fetchone()[0]
    for tag, row in rows.items():
        state = connection.execute("SELECT d.account_id,d.display_order,d.display_tag FROM account_link_metadata d "
                                   "JOIN accounts a ON a.id=d.account_id WHERE a.player_tag=?", (tag,)).fetchone()
        if state is None:
            continue
        if row is None:
            connection.execute("UPDATE account_link_metadata SET display_order=NULL,display_tag=NULL WHERE account_id=?",
                               (state["account_id"],))
        elif reorder or state["display_order"] is None:
            next_order += 1
            connection.execute("UPDATE account_link_metadata SET display_order=?,display_tag=? WHERE account_id=?",
                               (next_order, state["display_tag"] or tag, state["account_id"]))
