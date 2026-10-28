"""The legacy SQL and SQLite collation, under the independent links lock."""

from .store.legacy_links import LinkConflict


class LinkFacade:
    """Synchronous selectors used only inside worker-thread workflow reads."""

    def __init__(self, service):
        self.service = service

    def _read(self, select=None):
        if self.service is None:
            raise LinkConflict("links_topic_unavailable")
        return self.service.read_links_in_worker(select)

    def all(self):
        return self._read()

    def by_tag(self, tag):
        def select(connection):
            row = connection.execute("SELECT * FROM links WHERE player_tag = ?", (tag,)).fetchone()
            return dict(row) if row else None
        return self._read(select)

    def by_tags(self, tags):
        tags = tuple(dict.fromkeys(str(value) for value in tags))
        result = dict.fromkeys(tags)
        if not tags:
            return result
        def select(connection):
            connection.execute("BEGIN")
            for start in range(0, len(tags), 500):
                batch = tags[start:start + 500]
                rows = connection.execute(
                    f"SELECT * FROM links WHERE player_tag IN ({','.join('?' for _ in batch)}) ORDER BY player_tag", batch)
                for row in rows:
                    result[str(row["player_tag"])] = dict(row)
            return result
        return self._read(select)

    def for_user(self, user_id):
        def select(connection):
            return [dict(row) for row in connection.execute(
                "SELECT * FROM links WHERE discord_user_id=? "
                "ORDER BY COALESCE(player_name_last_seen,'') COLLATE NOCASE,player_tag", (int(user_id),))]
        return self._read(select)

    def for_members(self, member_ids):
        result = {value: [] for value in sorted(set(int(value) for value in member_ids))}
        if not result:
            return result
        ids = tuple(result)
        def select(connection):
            connection.execute("BEGIN")
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                for row in connection.execute(
                        f"SELECT * FROM links WHERE discord_user_id IN ({','.join('?' for _ in batch)}) "
                        "ORDER BY discord_user_id,player_tag", batch):
                    result[int(row["discord_user_id"])].append(dict(row))
            return result
        return self._read(select)
