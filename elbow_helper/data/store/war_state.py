"""War sources queue on their schedule rows; only failed items keep a receipt."""

from . import FamilyStore
from .projections import canonical_json


# A finished item leaves its schedule row exactly as an unqueued one.
CLEAR = "active_run_id=NULL,ordinal=NULL,queue_status=NULL,started_at=NULL"
# Outcomes a source's status can record; a private log is never a success.
OUTCOMES = {"ok", "not_in_war", "private_log", "failed"}

def enqueue(connection, run, ordinal, kind, scope, at):
    connection.execute(
        "INSERT INTO war_collection_state(source_kind,scope_id,next_due_at,active_run_id,ordinal,queue_status,started_at) "
        "VALUES (?,?,?,?,?,'pending',?) ON CONFLICT(source_kind,scope_id) DO UPDATE SET "
        "active_run_id=excluded.active_run_id,ordinal=excluded.ordinal,queue_status='pending',started_at=excluded.started_at",
        (kind, scope, at, run, ordinal, at))


def queue(connection, run, specs, at):
    for ordinal, (kind, scope) in enumerate(specs):
        enqueue(connection, run, ordinal, kind, scope, at)


def requested(connection, run, ordinal, at):
    connection.execute("UPDATE war_collection_state SET queue_status='running',started_at=? "
                       "WHERE active_run_id=? AND ordinal=? AND queue_status='pending'", (at, run, ordinal))


def queued(connection, run, ordinal, kind, scope):
    state = connection.execute(
        "SELECT * FROM war_collection_state WHERE source_kind=? AND scope_id=? AND active_run_id=? AND ordinal=?",
        (kind, scope, run, ordinal)).fetchone()
    if state is None:
        raise ValueError("war_source_not_queued")
    return state


def outcome(connection, run, state, status, at, *, error=None, response=None, count=None, written=0, complete=True,
            observed=None, label=None):
    """Close the item and record the check; `observed` replaces the last observation when given."""
    label = label or ("failed" if status == "failed" else "ok")
    if label not in OUTCOMES:
        raise ValueError("invalid_war_outcome")
    connection.execute(
        "UPDATE collection_runs SET attempted=attempted+1,succeeded=succeeded+?,empty=empty+?,failed=failed+?,"
        "rows_written=rows_written+? WHERE id=?",
        (status == "ok", status == "empty", status == "failed", written, run))
    success = label in {"ok", "not_in_war"}
    connection.execute(
        f"UPDATE war_collection_state SET {CLEAR},last_attempt_at=?,last_outcome=?,last_http_status=?,last_run_id=?,"
        "last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,observed_json=COALESCE(?,observed_json) "
        "WHERE source_kind=? AND scope_id=?",
        (at, label, response.status if response else None, run, success, at,
         canonical_json(observed) if observed is not None else None, state["source_kind"], state["scope_id"]))
    if status == "failed":
        connection.execute(
            "INSERT INTO collection_items(run_id,ordinal,source_kind,scope_id,status,started_at) "
            "VALUES (?,?,?,?,'failed',?)",
            (run, state["ordinal"], state["source_kind"], state["scope_id"], state["started_at"]))
        item = FamilyStore.item(connection, run, state["ordinal"])["id"]
        FamilyStore.outcome(connection, item, status, at, error=error, response=response,
                            count=count, written=written, complete=complete)


def finish(connection, run, at, *, interrupted=False):
    # Pending sources need no receipt: they stay due for the next pass.
    running = connection.execute("SELECT * FROM war_collection_state WHERE active_run_id=? AND queue_status='running'",
                                 (run,)).fetchall()
    for state in running:
        outcome(connection, run, state, "failed", at, error="interrupted" if interrupted else "unfinished", complete=False)
    pending = connection.execute("SELECT COUNT(*) FROM war_collection_state WHERE active_run_id=?", (run,)).fetchone()[0]
    connection.execute(f"UPDATE war_collection_state SET {CLEAR} WHERE active_run_id=?", (run,))
    connection.execute("UPDATE collection_runs SET skipped=skipped+? WHERE id=?", (pending, run))
    row = connection.execute("SELECT * FROM collection_runs WHERE id=?", (run,)).fetchone()
    ok, empty, failed, skipped = row["succeeded"], row["empty"], row["failed"], row["skipped"]
    status = ("interrupted" if interrupted else "partial" if failed + skipped and ok + empty else
              "failed" if failed + skipped else "empty" if not ok else "ok")
    connection.execute("UPDATE collection_runs SET finished_at=?,status=? WHERE id=?", (at, status, run))
    return {"status": status, "failed": failed, "succeeded": ok, "empty": empty}
