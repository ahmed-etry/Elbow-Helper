"""Dated profile facts with explicit collector and old-source coverage; no legacy-file reads."""

import json

from ..store.player_projection import COUNTERS
from ..store.profile_import_plan import TOTALS
from .core import RecordReads, page, pagination, tags, timestamp
from .coverage import signal


def counter_values(connection, account_id, at, *, conflicts=None):
    """Find each field's own last supplied sample; sparse imports never carry values forward."""
    fields, observed, sources = {}, {}, {}
    for index, (key, column) in enumerate(COUNTERS.items()):
        row = connection.execute(
            f"SELECT {column},sampled_at,field_presence_json FROM player_counters WHERE account_id=? AND sampled_at<=? "
            "AND ((json_type(field_presence_json,'$.fields')='integer' AND "
            "(json_extract(field_presence_json,'$.fields') & ?) != 0) OR "
            "EXISTS (SELECT 1 FROM json_each(field_presence_json,'$.fields') WHERE value=?)) "
            "ORDER BY sampled_at DESC LIMIT 1", (account_id, at, 1 << index, key)).fetchone()
        if row:
            fields[key], observed[key] = row[0], row[1]
            presence = json.loads(row[2])
            sources[key] = presence.get("sources", {}).get(key, "player_profile")
            if conflicts is not None and key in presence.get("conflicts", {}):
                conflicts[key] = {"values": presence["conflicts"][key], "observed_at": row[1], "source": sources[key]}
    row = connection.execute(
        "SELECT legend_counters_json,sampled_at,field_presence_json FROM player_counters WHERE account_id=? "
        "AND sampled_at<=? AND json_extract(field_presence_json,'$.legendStatistics')=1 "
        "ORDER BY sampled_at DESC LIMIT 1", (account_id, at)).fetchone()
    if row:
        fields["legendStatistics.fast"], observed["legendStatistics.fast"] = json.loads(row[0]), row[1]
        sources["legendStatistics.fast"] = json.loads(row[2]).get("sources", {}).get("legendStatistics", "player_profile")
    return fields, observed, sources


def observation_source(row):
    return ("legacy_health" if row["field_key"] == "townHallLevel" else "legacy_ranked") if (
        row["collector"] == "profile_import") else "player_profile"


def old_coverage(connection, identifier):
    return [{key: json.loads(value) if key.endswith("_json") else value
             for key, value in dict(row).items() if key not in {"account_id", "source_run_id"}}
            for row in connection.execute("SELECT * FROM profile_history_coverage WHERE account_id=? ORDER BY origin",
                                          (identifier,))]


def snapshot(connection, tag, at):
    account = connection.execute("SELECT * FROM accounts WHERE player_tag=?", (tag,)).fetchone()
    fields, observed, sources, totals, old_sources, conflicts = {}, {}, {}, {}, [], {}
    if account:
        identifier = account["id"]
        fields, observed, sources = counter_values(connection, identifier, at, conflicts=conflicts)
        for row in connection.execute(
                "SELECT p.*,r.collector FROM player_observations p JOIN collection_runs r ON r.id=p.source_run_id "
                "WHERE p.account_id=? AND p.observed_at<=? AND p.observed_at=(SELECT MAX(observed_at) "
                "FROM player_observations WHERE account_id=p.account_id AND field_key=p.field_key AND observed_at<=?)",
                (identifier, at, at)):
            key = row["field_key"]
            fields[key], observed[key], sources[key] = json.loads(row["value_json"]), row["observed_at"], observation_source(row)
        for key in TOTALS:
            row = connection.execute(
                f"SELECT {key},observed_at,origin FROM old_profile_totals WHERE account_id=? AND observed_at<=? "
                f"AND {key} IS NOT NULL ORDER BY observed_at DESC LIMIT 1", (identifier, at)).fetchone()
            if row:
                totals[key] = {"value": row[0], "observed_at": row[1], "source": row[2]}
        # These are the imported file's bounds, not observations at the requested instant.
        old_sources = old_coverage(connection, identifier)
    collector = signal(connection, "player_profile", tag, at, account_id=account["id"] if account else None)
    return {"player_tag": tag, "as_of": at, "fields": fields, "field_observed_at": observed,
            "field_sources": sources, "field_conflicts": conflicts, "old_totals": totals,
            "first_observed_at": account["first_observed_at"] if account else None,
            "coverage": {"collector": collector, "old_sources": old_sources,
                         "used_sources": sorted(set(sources.values()) | {r["source"] for r in totals.values()}),
                         "continuous_state_known": False}}


def history(connection, tag, after, before, at, limit, offset):
    account = connection.execute("SELECT id FROM accounts WHERE player_tag=?", (tag,)).fetchone()
    rows = []
    if account:
        rows = [dict(row) for row in connection.execute(
            "SELECT sampled_at AS observed_at,'counters' AS kind FROM player_counters WHERE account_id=? "
            "AND sampled_at>=? AND sampled_at<=? UNION "
            "SELECT observed_at,'fields' FROM player_observations WHERE account_id=? AND observed_at>=? AND observed_at<=? UNION "
            "SELECT observed_at,'old_totals' FROM old_profile_totals WHERE account_id=? AND observed_at>=? AND observed_at<=? "
            "ORDER BY observed_at,kind LIMIT ? OFFSET ?",
            (account[0], after, min(before, at), account[0], after, min(before, at), account[0], after, min(before, at),
             limit + 1, offset))]
    result = page(rows, limit, offset)
    for event in result["rows"]:
        stamp = event["observed_at"]
        if event["kind"] == "counters":
            row = connection.execute("SELECT * FROM player_counters WHERE account_id=? AND sampled_at=?",
                                     (account[0], stamp)).fetchone()
            presence = json.loads(row["field_presence_json"])
            bits = presence.get("fields", 0)
            event["fields"] = {key: row[column] for i, (key, column) in enumerate(COUNTERS.items())
                               if (key in bits if isinstance(bits, list) else bool(bits & (1 << i)))}
            if presence.get("legendStatistics"):
                event["fields"]["legendStatistics.fast"] = json.loads(row["legend_counters_json"])
            origins = presence.get("sources", {})
            event["sources"] = {
                key: origins.get("legendStatistics" if key == "legendStatistics.fast" else key, "player_profile")
                for key in event["fields"]}
            event["reset_context"] = json.loads(row["reset_context_json"])
            event["conflicts"] = presence.get("conflicts", {})
        elif event["kind"] == "fields":
            facts = connection.execute("SELECT p.*,r.collector FROM player_observations p JOIN collection_runs r "
                                       "ON r.id=p.source_run_id WHERE account_id=? AND observed_at=?", (account[0], stamp)).fetchall()
            event["fields"] = {row["field_key"]: json.loads(row["value_json"]) for row in facts}
            event["sources"] = {row["field_key"]: observation_source(row) for row in facts}
        else:
            row = connection.execute("SELECT * FROM old_profile_totals WHERE account_id=? AND observed_at=?",
                                     (account[0], stamp)).fetchone()
            event["fields"] = {key: row[key] for key in TOTALS if row[key] is not None}
            event["sources"] = {key: row["origin"] for key in event["fields"]}
    result.update(player_tag=tag, as_of=at, coverage=snapshot(connection, tag, at)["coverage"])
    return result


class ProfileReads(RecordReads):
    async def snapshot(self, player_tag, *, as_of=None):
        return await self._read(snapshot, tags([player_tag])[0], timestamp(as_of))

    async def history(self, player_tag, *, after, before, as_of=None, limit=50, offset=0):
        pagination(limit, offset)
        after, before, at = timestamp(after), timestamp(before), timestamp(as_of)
        if before < after:
            raise ValueError("Expected ordered history bounds")
        return await self._read(history, tags([player_tag])[0], after, before, at, limit, offset)
