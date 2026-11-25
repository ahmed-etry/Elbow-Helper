"""Dated profile facts with explicit collector and old-source coverage; no legacy-file reads."""

import json

from ..store.player_projection import COUNTERS


def counter_values(connection, account_id, at, *, conflicts=None, only=None):
    """Find each field's own last supplied sample; sparse imports never carry values forward."""
    fields, observed, sources = {}, {}, {}
    for index, (key, column) in enumerate(COUNTERS.items()):
        if only is not None and key not in only:
            continue
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
    if only is not None:
        return fields, observed, sources
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
