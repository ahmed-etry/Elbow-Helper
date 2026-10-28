"""Bounded player checkpoints and merged coverage, instead of hourly receipts."""

import json

from .player_projection import COUNTERS, HOUR_US
from .projections import canonical_json


def queue(connection, run, specs, at):
    for ordinal, (_, tag) in enumerate(specs):
        account_id = connection.execute("SELECT id FROM accounts WHERE player_tag=?", (tag,)).fetchone()[0]
        connection.execute(
            "INSERT INTO player_collection_state(account_id,active_run_id,ordinal,queue_status,started_at) "
            "VALUES (?,?,?,'pending',?) ON CONFLICT(account_id) DO UPDATE SET active_run_id=excluded.active_run_id,"
            "ordinal=excluded.ordinal,queue_status='pending',started_at=excluded.started_at", (account_id, run, ordinal, at))


def requested(connection, run, ordinal, at):
    connection.execute("UPDATE player_collection_state SET queue_status='running',started_at=? "
                       "WHERE active_run_id=? AND ordinal=? AND queue_status='pending'", (at, run, ordinal))


def evidence(connection, run, ordinal, account_id, tag, at):
    connection.execute(
        "INSERT INTO collection_items(run_id,ordinal,source_kind,scope_id,account_id,status,started_at) "
        "VALUES (?,?,'player_profile',?,?,'running',?) ON CONFLICT(run_id,ordinal) DO NOTHING",
        (run, ordinal, tag, account_id, at))
    return connection.execute("SELECT id FROM collection_items WHERE run_id=? AND ordinal=?", (run, ordinal)).fetchone()[0]


def outcome(connection, run, account_id, at, status, written, *, missing=False):
    connection.execute(
        "UPDATE collection_runs SET attempted=attempted+1,succeeded=succeeded+?,failed=failed+?,"
        "rows_written=rows_written+? WHERE id=?", (status == "ok", status != "ok", written, run))
    connection.execute(
        "UPDATE player_collection_state SET next_due_at=?,last_attempt_at=?,last_status=?,"
        "active_run_id=NULL,ordinal=NULL,queue_status=NULL WHERE account_id=?",
        (at + HOUR_US * (24 if missing else 1), at, status, account_id))


def finish(connection, run, at, *, interrupted=False):
    # Pending accounts need no persistent receipt. Only attempted failures do.
    running = connection.execute(
        "SELECT s.*,a.player_tag FROM player_collection_state s JOIN accounts a ON a.id=s.account_id "
        "WHERE active_run_id=? AND queue_status='running'", (run,)).fetchall()
    for row in running:
        item = evidence(connection, run, row["ordinal"], row["account_id"], row["player_tag"], row["started_at"])
        connection.execute("UPDATE collection_items SET status='failed',error_code=?,committed_at=? WHERE id=?",
                           ("interrupted" if interrupted else "unfinished", at, item))
        outcome(connection, run, row["account_id"], at, "failed", 0)
    pending = connection.execute("SELECT COUNT(*) FROM player_collection_state WHERE active_run_id=?", (run,)).fetchone()[0]
    connection.execute("UPDATE player_collection_state SET active_run_id=NULL,ordinal=NULL,queue_status=NULL "
                       "WHERE active_run_id=?", (run,))
    connection.execute("UPDATE collection_runs SET skipped=skipped+? WHERE id=?", (pending, run))
    row = connection.execute("SELECT * FROM collection_runs WHERE id=?", (run,)).fetchone()
    failed, skipped, ok = row["failed"], row["skipped"], row["succeeded"]
    status = ("interrupted" if interrupted else "partial" if failed + skipped and ok else
              "failed" if failed + skipped else "ok" if ok else "empty")
    connection.execute("UPDATE collection_runs SET finished_at=?,status=? WHERE id=?", (at, status, run))
    return {"status": status, "failed": failed, "succeeded": ok, "empty": 0}


def coverage(connection, account_id, run, at, completeness):
    encoded = canonical_json(completeness)
    last = connection.execute("SELECT * FROM player_coverage WHERE account_id=? ORDER BY from_at DESC LIMIT 1",
                              (account_id,)).fetchone()
    state = connection.execute("SELECT last_status FROM player_collection_state WHERE account_id=?", (account_id,)).fetchone()
    if (last and last["completeness_json"] == encoded and state[0] in {"ok", "partial"}
            and 0 <= at - last["last_observed_at"] <= 2 * HOUR_US):
        connection.execute("UPDATE player_coverage SET last_observed_at=?,last_run_id=? WHERE account_id=? AND from_at=?",
                           (at, run, account_id, last["from_at"]))
    else:
        connection.execute("INSERT INTO player_coverage VALUES (?,?,?,?,?,?)",
                           (account_id, at, at, encoded, run, run))


def migrate_state(connection):
    for account in connection.execute("SELECT id,last_profile_at FROM accounts").fetchall():
        account_id = account[0]
        items = connection.execute(
            "SELECT * FROM collection_items WHERE source_kind='player_profile' AND account_id=? "
            "ORDER BY fetched_at,id", (account_id,)).fetchall()
        if not items:
            continue
        connection.execute("INSERT INTO player_collection_state(account_id) VALUES (?)", (account_id,))
        last_good = {}
        for item in items:
            if item["fetched_at"] is None:
                connection.execute("UPDATE player_collection_state SET last_status='failed' WHERE account_id=?", (account_id,))
                continue
            complete = json.loads(item["completeness_json"])
            if complete.get("fields"):
                last_good["fields"] = item["fetched_at"]
                coverage(connection, account_id, item["run_id"], item["fetched_at"], complete)
            for section in ("arrays", "equipment"):
                values = complete.get(section, {})
                if isinstance(values, dict):
                    last_good.setdefault(section, {}).update({key: item["fetched_at"] for key, valid in values.items() if valid})
            connection.execute("UPDATE player_collection_state SET last_status=? WHERE account_id=?",
                               ("ok" if item["status"] == "ok" else "partial" if complete.get("fields") else "failed", account_id))
        last = items[-1]
        progress_at, achievements, context = _compact_legacy_counters(connection, account_id)
        at = last["fetched_at"] or last["started_at"]
        connection.execute(
            "UPDATE player_collection_state SET next_due_at=?,last_attempt_at=?,last_good_json=?,"
            "slow_sampled_at=?,progress_sampled_at=?,achievement_values_json=?,counter_context_json=? WHERE account_id=?",
            (at + HOUR_US * (24 if last["http_status"] == 404 else 1), at, canonical_json(last_good),
             account["last_profile_at"], progress_at, canonical_json(achievements), canonical_json(context), account_id))


def _compact_legacy_counters(connection, account_id):
    progress_at, achievements, context, previous = None, {}, {}, None
    for row in connection.execute("SELECT * FROM player_counters WHERE account_id=? ORDER BY sampled_at", (account_id,)).fetchall():
        at = row["sampled_at"]
        daily = progress_at is None or at // (24 * HOUR_US) > progress_at // (24 * HOUR_US)
        presence = json.loads(row["field_presence_json"])
        full = json.loads(row["achievement_values_json"])
        if not presence.get("arrays", {}).get("achievements", False):
            full = {**achievements, **full}
        diff = {key: full.get(key) for key in achievements.keys() | full.keys()
                if key not in full or key not in achievements or full[key] != achievements[key]} if daily else {}
        if daily:
            progress_at, achievements = at, full
        fields = presence.get("fields", [])
        if isinstance(fields, list):
            fields = sum(1 << index for index, key in enumerate(COUNTERS) if key in fields)
        encoded = canonical_json({"fields": fields, "legendStatistics": presence.get("legendStatistics", False)})
        signature = (*[row[column] for column in COUNTERS.values()], encoded,
                     row["legend_counters_json"], row["transient_item_fields_json"])
        if previous == signature and not diff:
            connection.execute("DELETE FROM player_counters WHERE account_id=? AND sampled_at=?", (account_id, at))
            continue
        reset = json.loads(row["reset_context_json"])
        current_context = {key: reset[key] for key in ("utc_month", "clan") if key in reset}
        if isinstance(current_context.get("clan"), dict):
            current_context["clan"] = current_context["clan"].get("tag")
        compact = {key: value for key, value in current_context.items() if key not in context or value != context[key]}
        compact.update({key: reset[key] for key in ("decreased_fields", "decreased_achievements") if reset.get(key)})
        connection.execute("UPDATE player_counters SET achievement_values_json=?,field_presence_json=?,reset_context_json=? "
                           "WHERE account_id=? AND sampled_at=?", (canonical_json(diff), encoded, canonical_json(compact), account_id, at))
        previous, context = signature, current_context
    return progress_at, achievements, context
