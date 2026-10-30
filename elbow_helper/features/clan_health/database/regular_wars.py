"""Stable identity and removal of superseded untagged regular-war snapshots."""

from __future__ import annotations

import sqlite3


def regular_war_identity(row) -> tuple[str, str, int] | None:
    """Missing preparation times cannot establish that two wars are the same."""
    clan = str(row.get("clan_tag") or "")
    opponent = str(row.get("opponent_tag") or "")
    preparation = int(row.get("preparation_start_ts") or 0)
    if (
        row.get("war_type") == "REG" and clan and opponent and preparation > 0
        and str(row.get("war_id") or "").startswith(f"REG:{clan}:")
    ):
        return clan, opponent, preparation
    return None


def matching_regular_wars(connection: sqlite3.Connection, identity) -> list[dict]:
    cursor = connection.execute(
        "SELECT war_id, clan_code, war_type, clan_tag, opponent_tag, "
        "preparation_start_ts, state, last_seen_ts FROM wars "
        "WHERE war_type = 'REG' AND clan_tag = ? AND opponent_tag = ? "
        "AND preparation_start_ts = ?",
        identity,
    )
    names = [column[0] for column in cursor.description]
    rows = [dict(zip(names, values)) for values in cursor.fetchall()]
    return [row for row in rows if regular_war_identity(row) == identity]


def delete_war_snapshot(connection: sqlite3.Connection, war_id: str, clan_code: str) -> dict[str, int]:
    """Delete dependent facts explicitly; deployed databases do not use cascades.

    Discover war-keyed tables so additional stored facts cannot be orphaned.
    The caller owns the transaction, including replacement snapshot writes.
    """
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )]
    counts = {}
    # Delete children before the war, including on databases with foreign keys.
    for table in sorted(tables, key=lambda name: name == "wars"):
        quoted = '"' + table.replace('"', '""') + '"'
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")}
        if "war_id" not in columns:
            continue
        where = "war_id = ?"
        params = [war_id]
        if "clan_code" in columns:
            where += " AND clan_code = ?"
            params.append(clan_code)
        counts[table] = connection.execute(f"DELETE FROM {quoted} WHERE {where}", params).rowcount
    return counts
