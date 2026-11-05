"""Legacy link selectors and SQL projection; neutral reads live in data.reads."""

import asyncio
from contextlib import closing
import sqlite3

from .store.legacy_links import TOPIC, FIELDS, LinkConflict
from .reads.core import RecordReads


def query_links(path):
    """Materialize the SQL tool's current-link table from a read-only record."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            try:
                rows = RecordReads._snapshot(connection, CurrentLinks._current_links, (None,))
            except LinkConflict as error:
                raise sqlite3.OperationalError("account_links_unavailable") from error
            columns = [(field, "INTEGER NOT NULL" if field == "discord_user_id" else
                        "TEXT PRIMARY KEY" if field == "player_tag" else "TEXT") for field in FIELDS]
            return columns, [tuple(row[field] for field in FIELDS) for row in rows.values()]
    raise RuntimeError('link_read_requires_worker')


def require_links(connection):
    if connection.execute("SELECT 1 FROM imports WHERE topic=?", (TOPIC,)).fetchone() is None:
        raise LinkConflict("people_import_not_started")
    if connection.execute("SELECT 1 FROM link_operations WHERE status='pending'").fetchone():
        raise LinkConflict("link_recovery_required")


class LinkSelectors:
    def all(self):
        return self._link_read()

    def by_tag(self, tag):
        def select(connection):
            row = connection.execute("SELECT * FROM current_link_rows WHERE player_tag=?", (tag,)).fetchone()
            return dict(row) if row else None
        return self._link_read(select)

    def by_tags(self, tags):
        tags = tuple(dict.fromkeys(str(value) for value in tags))
        result = dict.fromkeys(tags)
        def select(connection):
            for start in range(0, len(tags), 500):
                batch = tags[start:start + 500]
                for row in connection.execute(
                        f"SELECT * FROM current_link_rows WHERE player_tag IN ({','.join('?' for _ in batch)}) "
                        "ORDER BY player_tag", batch):
                    result[row["player_tag"]] = dict(row)
            return result
        return self._link_read(select)

    def for_user(self, user_id):
        def select(connection):
            return [dict(row) for row in connection.execute(
                "SELECT * FROM current_link_rows WHERE discord_user_id=? "
                "ORDER BY COALESCE(player_name_last_seen,'') COLLATE NOCASE,player_tag", (int(user_id),))]
        return self._link_read(select)

    def for_members(self, member_ids):
        result = {value: [] for value in sorted(set(int(value) for value in member_ids))}
        ids = tuple(result)
        def select(connection):
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                for row in connection.execute(
                        f"SELECT * FROM current_link_rows WHERE discord_user_id IN ({','.join('?' for _ in batch)}) "
                        "ORDER BY discord_user_id,player_tag", batch):
                    result[row["discord_user_id"]].append(dict(row))
            return result
        return self._link_read(select)


class CurrentLinks(LinkSelectors, RecordReads):
    def _link_read(self, select=None):
        return self.links_in_worker(select)

    def links_in_worker(self, select=None):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self.store._call(self._snapshot, (self._current_links, (select,)), {})
        raise RuntimeError("link_read_requires_worker")

    @staticmethod
    def _current_links(connection, select):
        require_links(connection)
        if select is not None:
            return select(connection)
        return {row["player_tag"]: dict(row) for row in connection.execute("SELECT * FROM current_link_rows")}
