"""Bounded polling checkpoints; successful polls need only run provenance."""

from . import FamilyStore


COLLECTORS = frozenset({"battle_logs", "league_history", "ranked_groups", "capital_raids", "reference_data"})
CLEAR = "active_run_id=NULL,ordinal=NULL,queue_status=NULL,started_at=NULL"


def queue(connection, run, specs, at):
    for ordinal, (kind, scope) in enumerate(specs):
        connection.execute(
            "INSERT INTO family_api_state(source_kind,scope_id,next_due_at,active_run_id,ordinal,queue_status,started_at) "
            "VALUES (?,?,?,?,?,'pending',?) ON CONFLICT(source_kind,scope_id) DO UPDATE SET "
            "active_run_id=excluded.active_run_id,ordinal=excluded.ordinal,queue_status='pending',started_at=excluded.started_at",
            (kind, scope, at, run, ordinal, at))


def requested(connection, run, ordinal, at):
    connection.execute("UPDATE family_api_state SET queue_status='running',started_at=? "
                       "WHERE active_run_id=? AND ordinal=? AND queue_status='pending'", (at, run, ordinal))


def outcome(connection, run, state, status, at, *, error=None, response=None, count=None, written=0):
    connection.execute(
        "UPDATE collection_runs SET attempted=attempted+1,succeeded=succeeded+?,empty=empty+?,failed=failed+?,"
        "rows_written=rows_written+?,origin_attempts=COALESCE(origin_attempts,0)+?,cache_hits=COALESCE(cache_hits,0)+? WHERE id=?",
        (status == "ok", status == "empty", status == "failed", written,
         response.attempts if response else 0, bool(response and response.attempts == 0), run))
    connection.execute(
        f"UPDATE family_api_state SET {CLEAR},last_attempt_at=?,last_status=?,last_http_status=?,last_run_id=? "
        "WHERE source_kind=? AND scope_id=?",
        (at, error or status, response.status if response else None, run, state["source_kind"], state["scope_id"]))
    if status == "failed":
        connection.execute(
            "INSERT INTO collection_items(run_id,ordinal,source_kind,scope_id,status,started_at) VALUES (?,?,?,?,'failed',?)",
            (run, state["ordinal"], state["source_kind"], state["scope_id"], state["started_at"]))
        item = FamilyStore.item(connection, run, state["ordinal"])["id"]
        FamilyStore.outcome(connection, item, status, at, error=error, response=response,
                            count=count, written=written, complete=False)


def finish(connection, run, at, *, interrupted=False):
    for state in connection.execute(
            "SELECT * FROM family_api_state WHERE active_run_id=? AND queue_status='running'", (run,)).fetchall():
        outcome(connection, run, state, "failed", at, error="interrupted" if interrupted else "unfinished")
    pending = connection.execute("SELECT COUNT(*) FROM family_api_state WHERE active_run_id=?", (run,)).fetchone()[0]
    connection.execute(f"UPDATE family_api_state SET {CLEAR} WHERE active_run_id=?", (run,))
    connection.execute("UPDATE collection_runs SET skipped=skipped+? WHERE id=?", (pending, run))
    row = connection.execute("SELECT * FROM collection_runs WHERE id=?", (run,)).fetchone()
    ok, empty, failed, skipped = (row[key] for key in ("succeeded", "empty", "failed", "skipped"))
    status = ("interrupted" if interrupted else "partial" if failed + skipped and ok + empty else
              "failed" if failed + skipped else "empty" if not ok else "ok")
    connection.execute("UPDATE collection_runs SET finished_at=?,status=? WHERE id=?", (at, status, run))
    return {"status": status, "failed": failed, "succeeded": ok, "empty": empty}
