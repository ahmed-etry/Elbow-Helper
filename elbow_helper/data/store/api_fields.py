"""Lossless leaf history beside typed facts, with compressed change-only patches.

Paths use JSON Pointer escaping. Array rows with unique API identities use an
``@`` selector (a JSON object); other arrays use their source index. This keeps
roster reordering from copying every player's fields. Empty containers are
values too. A compressed path dictionary gives each leaf a stable numeric key;
history patches keep only changed keys and values.
"""

from collections import Counter
import json
import zlib

from .player_projection import COUNTERS, HOUR_US, LEGEND_FAST, TRANSIENT
from .projections import CLAN_COUNTER_FIELDS, DONATION_FIELDS, canonical_json


def _pack(value):
    return zlib.compress(canonical_json(value).encode("utf-8"))


def unpack(value):
    return json.loads(zlib.decompress(value))


def _identity(row):
    if not isinstance(row, dict):
        return None
    for keys in (("tag",), ("name", "village", "info"), ("name", "village"), ("name",), ("id",), ("order",), ("endTime",)):
        if all(key in row and type(row[key]) in (str, int) for key in keys):
            return "@" + canonical_json({key: row[key] for key in keys})
    return None


def leaves(value, path="", route=()):
    """Yield (stable source path, leaf value, source field route)."""
    if isinstance(value, dict) and value:
        for key, item in value.items():
            escaped = key.replace("~", "~0").replace("/", "~1")
            yield from leaves(item, path + "/" + escaped, (*route, key))
    elif isinstance(value, list) and value:
        identities = [_identity(item) for item in value]
        counts = Counter(identities)
        for index, item in enumerate(value):
            identity = identities[index]
            selector = identity if identity is not None and counts[identity] == 1 else str(index)
            escaped = selector.replace("~", "~0").replace("/", "~1")
            yield from leaves(item, path + "/" + escaped, (*route, index))
    else:
        yield path, value, route


def _fast(kind, route):
    if not route:
        return False
    if kind == "player_profile":
        return (route[0] in COUNTERS or route[0] == "legendStatistics" and len(route) > 1 and route[1] in LEGEND_FAST
                or len(route) > 2 and route[0] in {"troops", "pets", "heroes", "spells", "heroEquipment"}
                and route[2] in TRANSIENT - {"equipment"})
    return kind == "clan_profile" and (route[0] in CLAN_COUNTER_FIELDS
            or route[0] == "memberList" and len(route) > 2 and route[2] in DONATION_FIELDS)


def collect_fields(connection, kind, scope, payload, run, at, *, complete=True):
    """Write every observed leaf, at the same cadence as its typed projection.

    Incomplete responses provide positive evidence only; absent paths cannot
    remove previous observations. War-log windows also never establish absence.
    The caller's transaction owns both these changes and typed facts.
    """
    canonical_json(payload)  # Validate the entire JSON before any writes.
    record = connection.execute("SELECT * FROM api_records WHERE source_kind=? AND scope_id=?", (kind, scope)).fetchone()
    paths, state = unpack(record["state_z"]) if record else ([], {})
    keys = {path: str(index) for index, path in enumerate(paths)}
    intervals = (HOUR_US, 24 * HOUR_US) if kind == "player_profile" else (HOUR_US, 0) if kind == "clan_profile" else (0, 0)
    clocks = [record[key] if record else None for key in ("fast_sampled_at", "slow_sampled_at")]
    if any(clock is not None and interval and at < clock for clock, interval in zip(clocks, intervals)):
        raise ValueError("out_of_order_observation")
    due = [clock is None or not interval or at // interval > clock // interval
           for clock, interval in zip(clocks, intervals)]
    if not any(due):
        return
    observed, changed, removed = set(), {}, []
    for path, value, route in leaves(payload):
        observed.add(path)
        group = 0 if _fast(kind, route) else 1
        if not due[group]:
            continue
        key = keys.get(path)
        if key is None:
            key = keys[path] = str(len(paths))
            paths.append(path)
        old = state.get(key)
        if old is not None and canonical_json(old[0]) == canonical_json(value):
            continue
        state[key] = [value, group]
        changed[key] = value
    if complete:
        for key in list(state):
            if paths[int(key)] in observed:
                continue
            _, group = state[key]
            if due[group]:
                removed.append(key)
                del state[key]
    if record is None:
        record_id = connection.execute(
            "INSERT INTO api_records(source_kind,scope_id,state_z) VALUES (?,?,?)",
            (kind, scope, _pack([[], {}]))).lastrowid
    else:
        record_id = record["id"]
    if changed or removed:
        connection.execute("INSERT INTO api_field_changes VALUES (?,?,?,?)",
                           (record_id, at, _pack([changed, removed]), run))
    if changed or removed or any(interval and ready for interval, ready in zip(intervals, due)):
        connection.execute("UPDATE api_records SET state_z=?,fast_sampled_at=?,slow_sampled_at=? WHERE id=?",
                           (_pack([paths, state]), at if due[0] else clocks[0], at if due[1] else clocks[1], record_id))


def read_fields(connection, kind, scope):
    """Resolve the path dictionary and replay history, preserving null vs absence."""
    record = connection.execute("SELECT id,state_z FROM api_records WHERE source_kind=? AND scope_id=?",
                                (kind, scope)).fetchone()
    if record is None:
        return {}
    paths, _ = unpack(record["state_z"])
    state = {}
    for row in connection.execute(
            "SELECT patch_z FROM api_field_changes WHERE record_id=? ORDER BY source_run_id", (record["id"],)):
        changed, removed = unpack(row[0])
        state.update(changed)
        for key in removed:
            state.pop(key, None)
    return {paths[int(key)]: value for key, value in state.items()}


def create_api_field_schema(connection):
    connection.execute("""CREATE TABLE player_ranked_references (
        account_id INTEGER NOT NULL REFERENCES accounts(id), observed_at INTEGER NOT NULL,
        current_group_tag TEXT, current_season_id INTEGER, previous_group_tag TEXT, previous_season_id INTEGER,
        source_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        PRIMARY KEY(account_id,observed_at))""")
    connection.execute("""CREATE TABLE api_records (
        id INTEGER PRIMARY KEY, source_kind TEXT NOT NULL, scope_id TEXT NOT NULL,
        state_z BLOB NOT NULL, fast_sampled_at INTEGER, slow_sampled_at INTEGER,
        UNIQUE(source_kind,scope_id))""")
    connection.execute("""CREATE TABLE api_field_changes (
        record_id INTEGER NOT NULL REFERENCES api_records(id), observed_at INTEGER NOT NULL,
        patch_z BLOB NOT NULL, source_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        PRIMARY KEY(record_id,source_run_id)) WITHOUT ROWID""")
