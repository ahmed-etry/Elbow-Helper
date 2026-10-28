"""Persistent war polling, using only the application's shared Clash client.

Nine clans: 2,592 current-war + 9 war-log logical GETs/day off season,
plus end checks, rollover recovery and paginated backfill. CWL days 1-11
add at most 9*288=2,592 group GETs/day and 1,440*V mutable relevant war
reads/day. V includes preparation wars; family mirrors reduce it.
Unknown round tags need one discovery GET each; unrelated pairings stop
there. Shared API Cache-Control reuse can reduce origin requests.
Each pass is one run row; routine polls share wall-clock slots, so all due
clans share that pass. Only failed items add a receipt.
"""

from datetime import datetime, timezone
import json
from urllib.parse import urlencode

from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction
from elbow_helper.data.store import now_us
from elbow_helper.data.store.war_state import enqueue
from elbow_helper.data.store.wars import SECOND, record_response


MAX_LOG_PAGES_PER_PASS = 10


def due_jobs(connection, clans, at):
    with sqlite_transaction(connection, immediate=True):
        window = datetime.fromtimestamp(at / SECOND, timezone.utc).day <= 11
        for clan in clans:
            for kind in ("current_war", "war_log", "cwl_group"):
                if kind == "cwl_group" and not window:
                    continue
                connection.execute("INSERT OR IGNORE INTO war_collection_state(source_kind,scope_id,next_due_at) VALUES (?,?,?)",
                                   (kind, clan.tag, at))
        rows = connection.execute(
            "SELECT s.*,w.end_at,w.final_at FROM war_collection_state s LEFT JOIN wars w ON w.id=s.war_id "
            "WHERE s.enabled=1 AND s.next_due_at<=? "
            "ORDER BY CASE s.source_kind WHEN 'current_war' THEN 0 WHEN 'cwl_group' THEN 1 WHEN 'cwl_war' THEN 2 ELSE 3 END,s.scope_id",
            (at,)).fetchall()
        return [dict(row) for row in rows if row["source_kind"] != "cwl_group" or window or connection.execute(
            "SELECT 1 FROM cwl_season_sources src JOIN cwl_seasons s ON s.id=src.season_id "
            "WHERE src.clan_tag=? AND s.final_at IS NULL", (row["scope_id"],)).fetchone()]


def append_item(connection, run, kind, scope, at):
    with sqlite_transaction(connection, immediate=True):
        ordinal = connection.execute("SELECT expected FROM collection_runs WHERE id=?", (run,)).fetchone()[0]
        enqueue(connection, run, ordinal, kind, scope, at)
        connection.execute("UPDATE collection_runs SET expected=expected+1 WHERE id=?", (run,))
        return ordinal


def path_for(job):
    kind, tag = job["source_kind"], encode_clash_tag(job["scope_id"])
    if kind == "cwl_war":
        return f"/clanwarleagues/wars/{tag}"
    if kind == "cwl_group":
        return f"/clans/{tag}/currentwar/leaguegroup"
    if kind == "war_log":
        query = {"limit": 50}
        after = json.loads(job["checkpoint_json"]).get("after")
        if after:
            query["after"] = after
        return f"/clans/{tag}/warlog?{urlencode(query)}"
    return f"/clans/{tag}/currentwar"


async def collect_pass(owner, run, initial):
    family_tags = {clan.tag for clan in owner.clans}
    queue = list(enumerate(initial))
    seen = set()
    pages = {}
    while queue:
        ordinal, job = queue.pop(0)
        kind, scope = job["source_kind"], job["scope_id"]
        seen.add((kind, scope))
        await owner.store.requested(run, ordinal)
        try:
            final = job.get("end_at") is not None and job.get("final_at") is None and now_us() >= job["end_at"] + 90 * SECOND
            response = await owner.clash_client.get(path_for(job), **({"fresh": True} if final else {}))
        except Exception:
            response = None
        await owner.store.call(record_response, run, ordinal, kind, scope, response, family_tags, now_us())
        if kind == "war_log":
            pages[scope] = pages.get(scope, 0) + 1
        # Group responses can reveal later rounds; current-war rollover can
        # make a log due now. Include both in the same run, without refetching
        # a tag already processed in this pass. Bound historical page work.
        pending = {(row["source_kind"], row["scope_id"]) for _, row in queue}
        for row in await owner.store.call(due_jobs, owner.clans, now_us()):
            key = row["source_kind"], row["scope_id"]
            page = row["source_kind"] == "war_log" and pages.get(row["scope_id"], 0) < MAX_LOG_PAGES_PER_PASS
            if key in pending or key in seen and not page:
                continue
            next_ordinal = await owner.store.call(append_item, run, *key, now_us())
            queue.append((next_ordinal, row))
            pending.add(key)
