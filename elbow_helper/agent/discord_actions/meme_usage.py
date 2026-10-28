"""Server-wide delivered meme history and freshness policy."""

import asyncio
import logging
import sqlite3
import time

from elbow_helper.infrastructure.persistence import sqlite_transaction

LOGGER = logging.getLogger(__name__)
RETENTION_SECONDS = 30 * 24 * 60 * 60


def create_meme_usage(connection):
    connection.execute("""CREATE TABLE meme_usage (
        usage_id INTEGER PRIMARY KEY,
        guild_id INTEGER NOT NULL,
        item_id TEXT NOT NULL,
        clip_family TEXT NOT NULL,
        channel_id INTEGER NOT NULL,
        posted_at REAL NOT NULL
    )""")
    connection.execute("CREATE INDEX meme_usage_server ON meme_usage(guild_id, posted_at)")


class MemeUsageStore:
    """Use the agent repository's connection and transaction conventions."""

    @staticmethod
    def _prune_memes(connection, guild_id, now):
        connection.execute("DELETE FROM meme_usage WHERE posted_at < ?", (now - RETENTION_SECONDS,))
        connection.execute("""DELETE FROM meme_usage WHERE guild_id=? AND usage_id NOT IN (
            SELECT usage_id FROM meme_usage WHERE guild_id=?
            ORDER BY posted_at DESC, usage_id DESC LIMIT 500
        )""", (guild_id, guild_id))

    def record_meme(self, *, guild_id, item_id, clip_family, channel_id, now=None):
        current = time.time() if now is None else now
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            connection.execute("""INSERT INTO meme_usage
                (guild_id, item_id, clip_family, channel_id, posted_at) VALUES (?, ?, ?, ?, ?)
            """, (guild_id, item_id, clip_family, channel_id, current))
            self._prune_memes(connection, guild_id, current)

    def recent_memes(self, *, guild_id, now=None):
        current = time.time() if now is None else now
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            self._prune_memes(connection, guild_id, current)
            rows = connection.execute("""SELECT item_id, clip_family, channel_id, posted_at
                FROM meme_usage WHERE guild_id=? ORDER BY posted_at DESC, usage_id DESC
            """, (guild_id,)).fetchall()
        return [dict(row) for row in rows]


def usage_repository(context):
    return (getattr(context, "meme_usage_repository", None)
            or getattr(context, "action_repository", None))


async def record_delivered_meme(repository, *, guild_id, channel_id, url, item_id=None):
    """Record a confirmed library delivery; storage failure must not resend the reply."""
    from .gif_library import ITEM_BY_ID, clip_family

    if repository is None:
        return
    matches = [key for key, item in ITEM_BY_ID.items()
               if url in (item["media"], "attachment://" + item["media"])]
    item_id = item_id if item_id in matches else next(iter(matches), None)
    if item_id is None:
        return
    try:
        await asyncio.to_thread(repository.record_meme, guild_id=guild_id, item_id=item_id,
                                clip_family=clip_family(item_id), channel_id=channel_id)
    except (sqlite3.Error, OSError):
        LOGGER.exception("Could not record delivered agent meme: item=%s", item_id)


def recent_memes(context):
    repository = usage_repository(context)
    if repository is None:
        return []
    try:
        return repository.recent_memes(guild_id=context.guild.id)
    except (sqlite3.Error, OSError):
        LOGGER.exception("Could not read agent meme usage")
        return []


def recent_meme_context(context):
    ids = [row["item_id"] for row in recent_memes(context)[:20]]
    if not ids:
        return ""
    return ("Memes you posted recently; when another fits as well, pick a fresh one. "
            + ", ".join(ids))


def select_meme(shortlist, *, requested=False, send_fit=4, usage=(), now=None, excluded=()):
    """Order good fits, skip recent families, then try a never-used sibling clip."""
    from .gif_library import ITEM_BY_ID, clip_family

    current = time.time() if now is None else now
    last_used = {}
    for row in usage:
        last_used[row["item_id"]] = max(last_used.get(row["item_id"], float('-inf')), row["posted_at"])
    candidates = sorted((item for item in shortlist if requested or item["fit"] >= send_fit),
                        key=lambda item: (-item["fit"], last_used.get(item["id"], float('-inf'))))
    if not candidates:
        return None, []
    if requested:
        return candidates[0]["id"], []
    ordered_usage = sorted(usage, key=lambda row: row["posted_at"], reverse=True)
    recent_families = {row["clip_family"] for index, row in enumerate(ordered_usage)
                       if index < 30 or row["posted_at"] >= current - 24 * 60 * 60}
    skipped = []
    for item in candidates:
        if clip_family(item["id"]) not in recent_families:
            return item["id"], skipped
        skipped.append(item["id"])
    top = candidates[0]["id"]
    alternate = next((item_id for item_id in ITEM_BY_ID
                      if item_id != top and clip_family(item_id) == clip_family(top)
                      and item_id not in last_used and item_id not in excluded), None)
    return alternate, skipped
