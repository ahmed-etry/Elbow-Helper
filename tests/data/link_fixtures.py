"""Synthetic nine-clan bootstrap for facade and workflow integration tests."""

from contextlib import closing
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from elbow_helper.data.links import LinkService
from elbow_helper.data.store import FamilyStore, now_us
from elbow_helper.data.store.clans import collect_clans
from elbow_helper.infrastructure.clash import ClashResponse

CLANS = {f"S{i}": SimpleNamespace(code=f"S{i}", tag=f"#PP{char}", name=f"Synthetic clan {i}")
         for i, char in enumerate("0289PYLQG")}


async def attach_links(owner):
    with owner._db_connect() as connection:
        path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    store = FamilyStore(path.parent / "family.sqlite3")
    await store.initialize()
    at = now_us()
    clans = tuple(CLANS.values())
    specs = [(kind, clan.tag) for clan in clans for kind in ("clan_profile", "clan_roster")]
    run = await store.begin_run("clans", "family_clans", specs, at)
    responses = [ClashResponse(200, {"tag": clan.tag, "name": clan.name, "members": 0, "memberList": []}, {}, 1, 0)
                 for clan in clans]
    await store.call(collect_clans, run, clans, responses, at)
    await store.finish(run)
    owner.link_service = LinkService(store, path)
    with patch("elbow_helper.data.store.people_import.CLANS", CLANS):
        await owner.link_service.startup()
    return owner.link_service


class FrozenLegacyLinks:
    """Read-only reference for the pre-switch SQL and its exact row ordering."""

    def __init__(self, path):
        self.path = Path(path)

    def get_all_links(self):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as c:
            c.row_factory = sqlite3.Row
            return {r["player_tag"]: dict(r) for r in c.execute("SELECT * FROM links")}

    def get_links_by_tags(self, tags):
        rows = self.get_all_links()
        return {str(tag): rows.get(str(tag)) for tag in dict.fromkeys(tags)}

    def get_links_for_members(self, members):
        result = {int(v): [] for v in sorted(set(members))}
        for row in sorted(self.get_all_links().values(), key=lambda r: (r["discord_user_id"], r["player_tag"])):
            if row["discord_user_id"] in result:
                result[row["discord_user_id"]].append(row)
        return result

    def get_links_for_user(self, member):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute("SELECT * FROM links WHERE discord_user_id=? "
                        "ORDER BY COALESCE(player_name_last_seen,'') COLLATE NOCASE,player_tag", (member,))]

    def get_link_by_tag(self, tag):
        return self.get_all_links().get(tag)
