"""One-time data migrations for the existing clan-health schema."""

from __future__ import annotations

from collections import defaultdict
import logging
import sqlite3

from elbow_helper.domain.cwl import cwl_season_key


LOGGER = logging.getLogger(__name__)


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
        key = cwl_season_key(wars)
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
