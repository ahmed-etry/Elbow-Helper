"""Compact battle identities and facts; generic history holds future fields.

String IDs share tags, names, armies and resource names. Resource arrays retain
their order as compressed (name ID, amount) pairs. Only corrections need a
previous typed version; unchanged rolling-window entries write nothing.
"""

import hashlib
import json
import zlib

from elbow_helper.domain.player_tags import normalize_player_tag
from .projections import canonical_json
from .war_projection import api_time


STRINGS = {"battleType": "battle_type_id", "opponentName": "opponent_name_id", "armyShareCode": "army_id"}
NUMBERS = {"opponentTownHallLevel": "opponent_town_hall", "stars": "stars",
           "destructionPercentage": "destruction_percentage", "battleTime": "duration"}
RESOURCES = ("lootedResources", "extraLootedResources", "availableLoot")
OPTIONAL = (*STRINGS, *NUMBERS, *RESOURCES)
STATE = ("timestamp_text", "presence", *STRINGS.values(), *NUMBERS.values())


def _pack(value):
    return zlib.compress(canonical_json(value).encode("utf-8"))


def identity(raw):
    if (not isinstance(raw, dict) or api_time(raw.get("battleTimestamp")) is None
            or type(raw.get("attack")) is not bool or normalize_player_tag(raw.get("opponentPlayerTag")) is None):
        raise ValueError("invalid_battle_identity")
    return canonical_json([raw["battleTimestamp"], normalize_player_tag(raw["opponentPlayerTag"]), raw["attack"]])


def _string(connection, value):
    row = connection.execute("SELECT id FROM battle_strings WHERE value=?", (value,)).fetchone()
    if row:
        return row[0]
    return connection.execute("INSERT INTO battle_strings(value) VALUES (?)", (value,)).lastrowid


def _find(connection, account, timestamp, opponent, attack):
    return connection.execute(
        "SELECT b.* FROM family_battles b WHERE account_string_id=(SELECT id FROM battle_strings WHERE value=?) "
        "AND battle_at=? AND opponent_string_id=(SELECT id FROM battle_strings WHERE value=?) AND is_attack=?",
        (account, api_time(timestamp), opponent, attack)).fetchone()


def save_battle(connection, account, raw, run, at):
    """Keep one battle per account/time/opponent/direction, including corrections."""
    from .api_fields import collect_fields

    timestamp, opponent, attack = json.loads(identity(raw))
    old = _find(connection, account, timestamp, opponent, attack)
    digest = hashlib.sha256(canonical_json(raw).encode("utf-8")).digest()
    if old and old["content_hash"] == digest:
        return True
    values = {"timestamp_text": timestamp, "presence": 0, **dict.fromkeys((*STRINGS.values(), *NUMBERS.values()))}
    resources, extra = [None] * len(RESOURCES), dict(raw)
    for key in ("battleTimestamp", "attack"):
        extra.pop(key)
    if raw["opponentPlayerTag"] == opponent:
        extra.pop("opponentPlayerTag")
    for bit, key in enumerate(OPTIONAL):
        value = raw.get(key)
        valid = False
        if key in STRINGS and isinstance(value, str):
            values[STRINGS[key]], valid = _string(connection, value), True
        elif key in NUMBERS and type(value) is int and 0 <= value < 2**63:
            values[NUMBERS[key]], valid = value, True
        elif key in RESOURCES and isinstance(value, list) and all(
                isinstance(row, dict) and row.keys() == {"name", "amount"} and isinstance(row["name"], str)
                and type(row["amount"]) is int for row in value):
            resources[RESOURCES.index(key)] = [[_string(connection, row["name"]), row["amount"]] for row in value]
            valid = True
        if valid:
            values["presence"] |= 1 << bit
            extra.pop(key)
    packed_resources = _pack(resources)
    if old:
        prior = [*[old[key] for key in STATE], json.loads(zlib.decompress(old["resources_z"]))]
        connection.execute("INSERT INTO battle_changes VALUES (?,?,?,?,?)",
                           (old["id"], run, at, old["last_run_id"], _pack(prior)))
        battle = old["id"]
        connection.execute(
            f"UPDATE family_battles SET {','.join(key + '=?' for key in STATE)},resources_z=?,content_hash=?,last_run_id=? WHERE id=?",
            (*[values[key] for key in STATE], packed_resources, digest, run, battle))
    else:
        columns = ("account_string_id", "battle_at", "opponent_string_id", "is_attack", *STATE,
                   "resources_z", "content_hash", "first_run_id", "last_run_id")
        battle = connection.execute(
            f"INSERT INTO family_battles({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            (_string(connection, account), api_time(timestamp), _string(connection, opponent), attack,
             *[values[key] for key in STATE], packed_resources, digest, run, run)).lastrowid
    has_extra = connection.execute("SELECT 1 FROM api_records WHERE source_kind='battle_extra' AND scope_id=?", (str(battle),)).fetchone()
    if extra or has_extra:
        collect_fields(connection, "battle_extra", str(battle), extra, run, at)
    return old is not None


def read_battle_fields(connection, scope):
    """Expose typed and generic leaves through the existing coverage reader."""
    from .api_fields import leaves, read_fields

    account, key = json.loads(scope)
    timestamp, opponent, attack = json.loads(key)
    row = _find(connection, account, timestamp, opponent, attack)
    if row is None:
        return {}
    def string(identifier):
        return connection.execute("SELECT value FROM battle_strings WHERE id=?", (identifier,)).fetchone()[0]
    raw = {"battleTimestamp": row["timestamp_text"], "opponentPlayerTag": string(row["opponent_string_id"]),
           "attack": bool(row["is_attack"])}
    resources = json.loads(zlib.decompress(row["resources_z"]))
    for bit, key in enumerate(OPTIONAL):
        if not row["presence"] & (1 << bit):
            continue
        if key in STRINGS:
            raw[key] = string(row[STRINGS[key]])
        elif key in NUMBERS:
            raw[key] = row[NUMBERS[key]]
        else:
            raw[key] = [{"name": string(name), "amount": amount} for name, amount in resources[RESOURCES.index(key)]]
    result = {path: value for path, value, _ in leaves(raw)}
    result.update({path: value for path, value in read_fields(connection, "battle_extra", str(row["id"])).items() if path})
    return result


def create_battle_schema(connection):
    connection.execute("CREATE TABLE battle_strings (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE)")
    connection.execute("""CREATE TABLE family_battles (
        id INTEGER PRIMARY KEY, account_string_id INTEGER NOT NULL REFERENCES battle_strings(id),
        battle_at INTEGER NOT NULL, opponent_string_id INTEGER NOT NULL REFERENCES battle_strings(id),
        is_attack INTEGER NOT NULL, timestamp_text TEXT NOT NULL, presence INTEGER NOT NULL,
        battle_type_id INTEGER REFERENCES battle_strings(id), opponent_name_id INTEGER REFERENCES battle_strings(id),
        army_id INTEGER REFERENCES battle_strings(id), opponent_town_hall INTEGER, stars INTEGER,
        destruction_percentage INTEGER, duration INTEGER, resources_z BLOB NOT NULL, content_hash BLOB NOT NULL,
        first_run_id INTEGER NOT NULL REFERENCES collection_runs(id), last_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        UNIQUE(account_string_id,battle_at,opponent_string_id,is_attack))""")
    connection.execute("""CREATE TABLE battle_changes (
        battle_id INTEGER NOT NULL REFERENCES family_battles(id), source_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        observed_at INTEGER NOT NULL, prior_run_id INTEGER NOT NULL REFERENCES collection_runs(id), prior_state_z BLOB NOT NULL,
        PRIMARY KEY(battle_id,source_run_id)) WITHOUT ROWID""")
