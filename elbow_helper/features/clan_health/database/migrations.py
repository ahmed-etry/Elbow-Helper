"""One-time data migrations for the existing clan-health schema."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import logging
import sqlite3

from .regular_wars import delete_war_snapshot, regular_war_identity

LOGGER = logging.getLogger(__name__)


def deduplicate_regular_wars(connection: sqlite3.Connection) -> None:
    """Keep the ended copy, otherwise the most recently observed snapshot."""
    cursor = connection.execute("SELECT * FROM wars WHERE war_type = 'REG'")
    names = [column[0] for column in cursor.description]
    groups = defaultdict(list)
    for values in cursor.fetchall():
        row = dict(zip(names, values))
        identity = regular_war_identity(row)
        if identity is not None:
            groups[identity].append(row)
    counts = defaultdict(int)
    duplicate_groups = 0
    for rows in groups.values():
        if len(rows) < 2:
            continue
        duplicate_groups += 1
        rows.sort(key=lambda row: (
            row["state"] == "warEnded", int(row["last_seen_ts"] or 0),
            int(row["end_ts"] or 0), row["war_id"], row["clan_code"],
        ), reverse=True)
        for row in rows[1:]:
            for table, removed in delete_war_snapshot(connection, row["war_id"], row["clan_code"]).items():
                counts[table] += removed
    LOGGER.info(
        "Regular war duplicate migration: groups=%s wars_removed=%s attack_rows_removed=%s rows_removed=%s",
        duplicate_groups, counts["wars"], counts["war_attacks"], dict(counts),
    )


def _stored_season_key(wars) -> str:
    """How the old store keyed a league: its month from round 1's estimated start.

    Only upgrades of old clan health backups use it; June 2026's second league
    kept its -catchup key this way. New leagues take the collector's month.
    """
    starts = []
    for war in wars:
        round_number = war.get("cwl_round")
        if type(round_number) is not int or not 1 <= round_number <= 7 or not war.get("start_ts"):
            continue
        try:
            start = datetime.fromtimestamp(float(war["start_ts"]), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        starts.append(start - timedelta(days=round_number - 1))
    if not starts:
        return ""
    first = min(starts)
    return first.strftime("%Y-%m") + ("-catchup" if first.day > 10 else "")


def key_stored_cwl_seasons(connection: sqlite3.Connection) -> None:
    """Preserve raw labels and key existing leagues without rewriting their war data."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(wars)")}
    if "cwl_season_label" in columns:
        return
    connection.execute("ALTER TABLE wars ADD COLUMN cwl_season_label TEXT NOT NULL DEFAULT ''")
    groups = defaultdict(list)
    for war_id, clan, label, raw, start, round_number in connection.execute(
        "SELECT war_id, clan_code, cwl_season, cwl_season_label, start_ts, cwl_round "
        "FROM wars WHERE war_type = 'CWL'",
    ).fetchall():
        groups[(clan, label)].append({
            "war_id": war_id, "raw": raw, "start_ts": start, "cwl_round": round_number,
        })
    counts = defaultdict(lambda: [0, 0])
    for (clan, old_label), wars in groups.items():
        key = _stored_season_key(wars)
        if not key:
            LOGGER.warning("CWL season start unavailable: clan=%s old_label=%s", clan, old_label)
            key = old_label
        for war in wars:
            raw = war["raw"] or old_label
            if key != old_label or raw != war["raw"]:
                connection.execute(
                    "UPDATE wars SET cwl_season = ?, cwl_season_label = ? "
                    "WHERE war_id = ? AND clan_code = ?",
                    (key, raw, war["war_id"], clan),
                )
            counts[(old_label, key)][0] += int(key != old_label)
            counts[(old_label, key)][1] += int(raw != war["raw"])
    for (old_label, key), (changed, copied) in sorted(counts.items()):
        LOGGER.info(
            "CWL season migration: old_label=%s key=%s wars_changed=%s raw_labels_copied=%s",
            old_label, key, changed, copied,
        )
