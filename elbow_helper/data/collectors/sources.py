"""Sequential, bounded source passes through the application-owned client."""

import json
from urllib.parse import urlencode

from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction
from elbow_helper.data.store import now_us
from elbow_helper.data.store.sources import due_sources, record_source
from elbow_helper.data.store import source_state


MAX_PAGES_PER_PASS = 10


def path_for(job):
    kind, scope = job["source_kind"], job["scope_id"]
    if kind == "ranked_group":
        player, group, season = json.loads(scope)
        return f"/leaguegroup/{encode_clash_tag(group)}/{season}?{urlencode({'playerTag': player})}"
    if kind in {"battle_log", "league_history"}:
        suffix = "battlelog" if kind == "battle_log" else "leaguehistory"
        return f"/players/{encode_clash_tag(scope)}/{suffix}"
    if kind == "capital_raids":
        path = f"/clans/{encode_clash_tag(scope)}/capitalraidseasons"
    else:
        path = scope
    if kind == "goldpass":
        return path
    query = {"limit": 50 if kind == "capital_raids" else 1000}
    after = json.loads(job["checkpoint_json"]).get("after")
    if after:
        query["after"] = after
    return f"{path}?{urlencode(query)}"


def append_page(connection, run, job, at):
    kind, scope = job["source_kind"], job["scope_id"]
    with sqlite_transaction(connection, immediate=True):
        ordinal = connection.execute("SELECT expected FROM collection_runs WHERE id=?", (run,)).fetchone()[0]
        source_state.queue(connection, run, [(kind, scope)], at)
        connection.execute("UPDATE family_api_state SET ordinal=? WHERE source_kind=? AND scope_id=?", (ordinal, kind, scope))
        connection.execute("UPDATE collection_runs SET expected=expected+1 WHERE id=?", (run,))
        state = connection.execute("SELECT * FROM family_api_state WHERE source_kind=? AND scope_id=?", (kind, scope)).fetchone()
        return ordinal, {**job, **dict(state)}


async def collect_sources(owner, collector):
    async with owner._source_locks[collector]:
        if owner._closing:
            return
        jobs = await owner.store.call(due_sources, collector, owner.clans, now_us())
        if not jobs:
            return

        async def operation(run):
            for ordinal, initial in enumerate(jobs):
                job = initial
                for page in range(MAX_PAGES_PER_PASS):
                    await owner.store.requested(run, ordinal)
                    # A terminal read must not reuse the last mutable standings.
                    options = {"attempts": 1, "fresh": True} if job["source_kind"] == "ranked_group" and not job["current"] else {}
                    try:
                        response = await owner.clash_client.get(path_for(job), **options)
                    except Exception:
                        response = None
                    after = await owner.store.call(record_source, run, ordinal, job, response, now_us())
                    if not after or page == MAX_PAGES_PER_PASS - 1:
                        break
                    ordinal, job = await owner.store.call(append_page, run, job, now_us())

        specs = [(job["source_kind"], job["scope_id"]) for job in jobs]
        return await owner._run(collector, "family_api", specs, operation)
