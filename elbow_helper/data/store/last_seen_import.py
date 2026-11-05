"""Once-only copy of members' last messages from the old member lifecycle file.

Each copied time keeps the basis ``lifecycle_import``. A later Discord
observation replaces it, an older one never does. A finished copy is marked in
``imports`` and is never repeated; a failed one keeps nothing and runs at the
next start.
"""

from datetime import datetime, timezone
import json

from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from .legacy_links import digest
from .link_state import receipt
from .people import member
from .projections import canonical_json

TOPIC = "lifecycle_last_seen"
BASIS = "lifecycle_import"


def read_last_seen(path):
    """(member ID, message time, channel ID) rows and the count of unreadable entries."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], 0
    if not isinstance(data, dict):
        raise ValueError("Invalid member lifecycle file")
    entries = data.get("last_seen", {})
    if not isinstance(entries, dict):
        raise ValueError("Invalid member lifecycle file")
    rows, skipped = [], 0
    for key, value in entries.items():
        try:
            member_id, channel_id = int(key), value["channel_id"]
            seen = datetime.fromisoformat(value["ts_iso"])
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            at = int(seen.timestamp() * 1_000_000)
            if member_id <= 0 or type(channel_id) is not int or channel_id <= 0 or at <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            skipped += 1
            continue
        rows.append((member_id, at, channel_id))
    return sorted(rows), skipped


def import_last_seen(connection, path, at):
    """Copy the old file's last messages once; returns the checks, or None when already copied."""
    if connection.execute("SELECT 1 FROM imports WHERE topic=?", (TOPIC,)).fetchone():
        return None
    rows, skipped = read_last_seen(path)
    with sqlite_transaction(connection, immediate=True):
        item = receipt(connection, "last_seen_import", "member_lifecycle", at, len(rows))
        copied = kept = 0
        for discord_id, message_at, channel_id in rows:
            changed = connection.execute(
                "INSERT INTO member_last_messages(member_id,message_at,channel_id,basis,source_item_id) "
                "VALUES (?,?,?,?,?) ON CONFLICT(member_id) DO UPDATE SET message_at=excluded.message_at,"
                "channel_id=excluded.channel_id,basis=excluded.basis,source_item_id=excluded.source_item_id "
                "WHERE excluded.message_at>member_last_messages.message_at",
                (member(connection, discord_id, at), message_at, channel_id, BASIS, item)).rowcount
            copied += changed
            kept += not changed
        checks = {"entries": len(rows) + skipped, "copied": copied, "newer_observation_kept": kept,
                  "unreadable": skipped}
        connection.execute("INSERT INTO imports VALUES (?,?,?,?,?,?,?,?)", (
            TOPIC, 1, digest(rows), at, digest([]), at, canonical_json(checks), item))
    return checks
