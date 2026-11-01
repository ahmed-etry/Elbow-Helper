"""Frozen reads of the old regular-war data, grouped into the wars they describe.

Nothing here touches the family record. A bad row raises before any write;
every other source row is either carried by an observation or counted in the
ledger with the reason it is left out.
"""

from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from . import now_us
from .legacy_links import ImportMismatch, digest
from .war_projection import project_war
from .wars import SECOND

# Old clan health marks the end of a regular war's capture with these sources.
FINAL_SOURCES = ("currentwar_api:warEnded", "currentwar_api:warEnded_scheduled")
# Same window as the old attendance: a Clash pause moves a war's end, and its old ID, by up to this much.
CAPTURE_WINDOW = 72 * 3600 * SECOND
STATES = {"preparation": 0, "inWar": 1, "warEnded": 2}
KINDS = ("payload", "table", "participation")
HISTORY = "war_board_history"
TABLES = {
    "wars": (("war_id", "war_type", "clan_code", "clan_tag", "opponent_tag", "opponent_name", "team_size",
              "attacks_per_member", "state", "preparation_start_ts", "start_ts", "end_ts", "last_seen_ts"),
             ("war_id", "clan_code")),
    "war_roster_members": (("war_id", "clan_code", "player_tag", "player_name", "townhall", "map_position",
                            "attacks_expected", "attacks_used", "roster_state", "captured_ts", "source"),
                           ("war_id", "clan_code", "player_tag")),
    "war_attacks": (("war_id", "war_type", "clan_code", "clan_tag", "end_ts", "war_state", "player_tag",
                     "attack_order", "defender_tag", "defender_name", "defender_map_position",
                     "defender_townhall", "stars", "destruction", "fresh_attack", "duration", "source"),
                    ("war_id", "clan_code", "player_tag", "attack_order", "defender_tag")),
    "war_activity": (("war_id", "war_type", "clan_code", "clan_tag", "end_ts", "player_tag", "player_name",
                      "attacks_expected", "attacks_used", "stars", "destruction", "attack_count", "source"),
                     ("war_id", "clan_code", "player_tag")),
}


@dataclass(frozen=True)
class WarSnapshot:
    version: int
    digest: str
    frozen_at: int
    rows: dict
    history: dict


def snapshot_wars(health_path, cache_path=None, at=None):
    """Read both sources once, consistently and read-only; neither file is adopted or upgraded."""
    path = Path(health_path).resolve()
    rows = {}
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            with sqlite_transaction(connection):
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                for table, (columns, order) in TABLES.items():
                    found = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    if not set(columns) <= found:
                        raise ImportMismatch("legacy_schema_mismatch:" + table)
                    rows[table] = [dict(row) for row in connection.execute(
                        f"SELECT {','.join(columns)} FROM {table} ORDER BY {','.join(order)}")]
    except sqlite3.Error as error:
        raise ImportMismatch("legacy_unreadable") from error
    history = {}
    if cache_path is not None:
        try:
            history = json.loads(Path(cache_path).read_text(encoding="utf-8"))[HISTORY]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ImportMismatch("cache_unreadable") from error
        if not isinstance(history, dict):
            raise ImportMismatch("cache_unreadable")
    frozen_at = now_us() if at is None else at
    if type(frozen_at) is not int or frozen_at < 0:
        raise ValueError("invalid_freeze_time")
    try:
        content = digest({"version": version, "rows": rows, "history": history})
    except (TypeError, ValueError) as error:
        raise ImportMismatch("legacy_unreadable") from error
    return WarSnapshot(version, content, frozen_at, rows, history)


def bad(table):
    raise ImportMismatch("bad_row:" + table)


def count(value, minimum=0):
    return type(value) is int and value >= minimum


def number(value, maximum):
    return type(value) in (int, float) and 0 <= value <= maximum


def text(value):
    return isinstance(value, str)


def to_us(seconds):
    return seconds * SECOND or None


def state_of(source):
    """The war state an old capture source names, such as warEnded_scheduled."""
    state = source.partition(":")[2].partition("_")[0]
    return state if state in STATES else None


def participation_identity(war_id, clan_tag):
    """Regular war IDs are REG:<clan>:<end time>:<opponent> when Clash gives no war tag."""
    parts = war_id.split(":")
    if len(parts) != 4 or parts[0] != "REG":
        return None
    side, opponent = normalize_player_tag(parts[1]), normalize_player_tag(parts[3])
    if not side or side != normalize_player_tag(clan_tag) or not opponent:
        return None
    try:
        end = datetime.strptime(parts[2], "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return side, opponent, int(end.timestamp())


def lineup_entry(*, name, townhall, position, expected, used, fields, legacy, code, table):
    # The old store writes 0 where it did not know a value.
    return {"name": name, "townhall": townhall or None, "position": position or None, "expected": expected or None,
            "used": used, "extra": {"field_presence": fields, "legacy": legacy}, "rows": [(table, code)]}


def attack_entry(row, attacker, code):
    return {"attacker": attacker, "defender": normalize_player_tag(row["defender_tag"]), "stars": row["stars"],
            "destruction": row["destruction"], "duration": row["duration"], "stub": False,
            "extra": {"field_presence": ["attackerTag", "defenderTag", "destructionPercentage", "duration",
                                         "order", "stars"],
                      "legacy": {"defender_name": row["defender_name"] or None,
                                 "defender_townhall": row["defender_townhall"] or None,
                                 "defender_position": row["defender_map_position"] or None,
                                 "fresh_attack": bool(row["fresh_attack"]), "war_state": row["war_state"],
                                 "source": row["source"]}},
            "rows": [("war_attacks", code)]}


def activity_values(row):
    return {"stars": row["stars"], "destruction": row["destruction"], "attack_count": row["attack_count"],
            "source": row["source"]}


class Plan:
    """Observations of the old data, then the wars they describe (``events``)."""

    def __init__(self, frozen, clans, ledger):
        self.frozen, self.ledger = frozen, ledger
        self.tags = {code: normalize_player_tag(tag) for code, tag in clans.items()}
        self.observations, self.stored = [], {}
        self.old_wars = defaultdict(set)
        self.activity, self.attacks = defaultdict(list), defaultdict(list)
        self.table_wars()
        self.lineups_and_attacks()
        self.participation()
        self.payloads()

    def own(self, code, tag, table):
        if code not in self.tags or self.tags[code] != normalize_player_tag(tag):
            bad(table)
        return self.tags[code]

    def table_wars(self):
        for row in self.frozen.rows["wars"]:
            code = row["clan_code"]
            if row["war_type"] == "CWL":
                self.ledger["wars", code, "excluded:cwl"] += 1
                continue
            tag = self.own(code, row["clan_tag"], "wars") if row["war_type"] == "REG" else bad("wars")
            opponent = normalize_player_tag(row["opponent_tag"])
            prep, start, end = (to_us(row[key]) for key in ("preparation_start_ts", "start_ts", "end_ts"))
            if (not text(row["war_id"]) or not row["war_id"] or not opponent or opponent == tag
                    or row["state"] not in STATES or not text(row["opponent_name"])
                    or not all(count(row[key]) for key in ("team_size", "attacks_per_member", "preparation_start_ts",
                                                           "start_ts", "end_ts", "last_seen_ts"))
                    or not end or not row["last_seen_ts"] or prep and start and not prep <= start <= end):
                bad("wars")
            seen = row["last_seen_ts"] * SECOND
            observation = {
                "kind": "table", "code": code, "tag": tag, "opponent": opponent,
                "pair": tuple(sorted((tag, opponent))), "prep": prep, "start": start, "end": end,
                "state": row["state"], "team_size": row["team_size"] or None, "apm": row["attacks_per_member"] or None,
                "names": {opponent: row["opponent_name"] or None}, "seen": seen, "members": {}, "attacks": {},
                "aliases": [("clan_health", row["war_id"], code, "REG", row["state"], seen)],
                "rows": [("wars", code)]}
            self.stored[code, row["war_id"]] = observation
            self.observations.append(observation)
            self.old_wars[code].add(row["war_id"])

    def lineups_and_attacks(self):
        rows, cwl = self.frozen.rows, self.stored_cwl()
        for row in rows["war_roster_members"]:
            code, key, tag = row["clan_code"], (row["clan_code"], row["war_id"]), normalize_player_tag(row["player_tag"])
            if (not tag or not text(row["player_name"]) or row["roster_state"] not in STATES or not text(row["source"])
                    or not all(count(row[field]) for field in ("townhall", "map_position", "attacks_expected",
                                                               "attacks_used", "captured_ts"))):
                bad("war_roster_members")
            if key in cwl or key not in self.stored and str(row["war_id"]).startswith("CWL:"):
                self.ledger["war_roster_members", code, "excluded:cwl"] += 1
            elif key not in self.stored:
                self.ledger["war_roster_members", code, "excluded:no_war_row"] += 1
            else:
                observation = self.stored[key]
                observation["members"][observation["tag"], tag] = lineup_entry(
                    name=row["player_name"], townhall=row["townhall"], position=row["map_position"],
                    expected=row["attacks_expected"], used=row["attacks_used"], code=code, table="war_roster_members",
                    fields=["mapPosition", "name", "tag", "townhallLevel"],
                    legacy={"roster_state": row["roster_state"], "source": row["source"],
                            "captured_at": row["captured_ts"] * SECOND})
        for row in rows["war_activity"]:
            code, key, tag = row["clan_code"], (row["clan_code"], row["war_id"]), normalize_player_tag(row["player_tag"])
            if (row["war_type"] not in ("REG", "CWL") or not tag or not text(row["player_name"]) or not text(row["source"])
                    or not text(row["war_id"]) or not row["war_id"] or not count(row["end_ts"], 1)
                    or not all(count(row[field]) for field in ("attacks_expected", "attacks_used", "attack_count"))
                    or not number(row["stars"], 1_000_000) or not number(row["destruction"], 100_000_000)):
                bad("war_activity")
            if row["war_type"] == "CWL":
                self.ledger["war_activity", code, "excluded:cwl"] += 1
            elif key in self.stored:
                self.add_activity(self.stored[key], row, tag)
            else:
                self.own(code, row["clan_tag"], "war_activity")
                self.activity[key].append((row, tag))
        for row in rows["war_attacks"]:
            code, key = row["clan_code"], (row["clan_code"], row["war_id"])
            attacker, defender = normalize_player_tag(row["player_tag"]), normalize_player_tag(row["defender_tag"])
            if (row["war_type"] not in ("REG", "CWL") or not attacker or not defender or row["war_state"] not in STATES
                    or not count(row["attack_order"], 1) or type(row["stars"]) is not int or not 0 <= row["stars"] <= 3
                    or not number(row["destruction"], 100) or not count(row["duration"]) or not text(row["war_id"])
                    or not all(count(row[field]) for field in ("defender_map_position", "defender_townhall", "end_ts"))
                    or row["fresh_attack"] not in (0, 1) or not text(row["defender_name"]) or not text(row["source"])):
                bad("war_attacks")
            if row["war_type"] == "CWL":
                self.ledger["war_attacks", code, "excluded:cwl"] += 1
            elif key in self.stored:
                self.add_attack(self.stored[key], row, attacker)
            else:
                self.own(code, row["clan_tag"], "war_attacks")
                self.attacks[key].append((row, attacker))

    def stored_cwl(self):
        return {(row["clan_code"], row["war_id"]) for row in self.frozen.rows["wars"] if row["war_type"] == "CWL"}

    def add_attack(self, observation, row, attacker):
        entry = attack_entry(row, attacker, row["clan_code"])
        old = observation["attacks"].setdefault(row["attack_order"], entry)
        if old is not entry:
            self.ledger["war_attacks", row["clan_code"], "matched" if (old["attacker"], old["defender"], old["stars"],
                old["destruction"]) == (attacker, entry["defender"], entry["stars"], entry["destruction"]) else "conflict"] += 1

    def add_activity(self, observation, row, tag):
        code, entry = row["clan_code"], observation["members"].get((observation["tag"], tag))
        values = activity_values(row)
        if entry is None:
            observation["members"][observation["tag"], tag] = lineup_entry(
                name=row["player_name"], townhall=0, position=0, expected=row["attacks_expected"], used=row["attacks_used"],
                code=code, table="war_activity", fields=["name", "tag"],
                legacy={"activity": values, "from_activity": True})
        elif (entry["expected"], entry["used"]) != (row["attacks_expected"] or None, row["attacks_used"]):
            self.ledger["war_activity", code, "conflict"] += 1
        else:
            entry["extra"]["legacy"]["activity"] = values
            entry["rows"].append(("war_activity", code))

    def exclude(self, key, reason):
        for table, rows in (("war_activity", self.activity), ("war_attacks", self.attacks)):
            if rows.get(key):
                self.ledger[table, key[0], "excluded:" + reason] += len(rows[key])

    def participation(self):
        """Regular wars with final participation but no wars row, one per war, as the old attendance finds them.

        Captures within the pause window of each other, or of an ended war
        that has its own row, are the same war; the latest final capture
        supplies the lineup and attacks.
        """
        for key in self.attacks.keys() - self.activity.keys():
            # An attack list without any lineup of the same capture has no war to belong to.
            self.old_wars[key[0]].add(key[1])
            self.exclude(key, "no_lineup")
        by_side, unproven = defaultdict(list), []
        for (code, war_id), rows in self.activity.items():
            self.old_wars[code].add(war_id)
            identity = participation_identity(war_id, rows[0][0]["clan_tag"])
            if identity is None:
                unproven.append((code, war_id))
            else:
                by_side[code, identity[0], identity[1]].append((identity[2], war_id))
        for key in unproven:
            self.exclude(key, "unproven_identity")
        stored_ends = defaultdict(list)
        for (code, _), observation in self.stored.items():
            if observation["state"] == "warEnded":
                stored_ends[code, observation["tag"], observation["opponent"]].append(observation)
        window = CAPTURE_WINDOW // SECOND
        clusters = []
        for side, items in sorted(by_side.items()):
            items.sort()
            cluster = []
            for item in items:
                if cluster and item[0] - cluster[0][0] > window:
                    clusters.append((side, cluster))
                    cluster = []
                cluster.append(item)
            clusters.append((side, cluster))
        for (code, tag, opponent), items in clusters:
            war_ids = [war_id for _, war_id in items]
            final = {war_id: [(row, who) for row, who in self.activity[code, war_id] if row["source"] in FINAL_SOURCES]
                     for war_id in war_ids}
            final = {war_id: rows for war_id, rows in final.items() if rows}
            if not final:
                for war_id in war_ids:
                    self.exclude((code, war_id), "no_final_capture")
                continue
            aliases = [("clan_health", war_id, code, "REG", max(
                (state_of(row["source"]) for row, _ in self.activity[code, war_id] if state_of(row["source"])),
                key=STATES.get, default=None), None) for war_id in war_ids]
            nearest = min(((abs(stored["end"] // SECOND - end), stored["end"], id(stored), stored)
                           for stored in stored_ends[code, tag, opponent] for end, _ in items), default=None)
            if nearest is not None and nearest[0] <= window:
                # A capture of a war that has its own row only adds that war's IDs.
                nearest[3]["aliases"].extend(aliases)
                for war_id in war_ids:
                    self.exclude((code, war_id), "covered_by_stored_war")
                continue
            chosen = max(final, key=lambda war_id: (max(row["end_ts"] for row, _ in final[war_id]), war_id))
            end = max(row["end_ts"] for row, _ in final[chosen]) * SECOND
            observation = {
                "kind": "participation", "code": code, "tag": tag, "opponent": opponent,
                "pair": tuple(sorted((tag, opponent))), "prep": None, "start": None, "end": end, "state": "warEnded",
                "team_size": None, "apm": None, "names": {}, "seen": end, "members": {}, "attacks": {},
                "aliases": aliases, "rows": []}
            used = {id(row) for row, _ in final[chosen]}
            for row, who in final[chosen]:
                observation["members"][tag, who] = lineup_entry(
                    name=row["player_name"], townhall=0, position=0, expected=row["attacks_expected"],
                    used=row["attacks_used"], code=code, table="war_activity", fields=["name", "tag"],
                    legacy={"activity": activity_values(row), "from_activity": True})
            for war_id in war_ids:
                for row, _ in self.activity[code, war_id]:
                    if id(row) not in used:
                        self.ledger["war_activity", code, "excluded:" + (
                            "not_final_row" if war_id == chosen else "not_chosen_capture")] += 1
                for row, who in self.attacks[code, war_id]:
                    if war_id != chosen or row["war_state"] != "warEnded":
                        self.ledger["war_attacks", code, "excluded:" + (
                            "not_final_row" if war_id == chosen else "not_chosen_capture")] += 1
                    else:
                        self.add_attack(observation, row, who)
            self.observations.append(observation)

    def payloads(self):
        for code, slots in sorted(self.frozen.history.items()):
            if code not in self.tags or not isinstance(slots, dict):
                bad(HISTORY)
            for slot in ("current", "previous"):
                raw = slots.get(slot)
                if raw and not isinstance(raw, dict):
                    bad(HISTORY)
                if not raw or raw.get("state") == "notInWar":
                    self.ledger[HISTORY, code, "excluded:" + ("not_in_war" if raw else "empty")] += 1
                    continue
                try:
                    war = project_war(raw)
                except (ValueError, TypeError, AttributeError, OverflowError):
                    bad(HISTORY)
                tags = [side["clan_tag"] for side in war["sides"]]
                if self.tags[code] not in tags:
                    bad(HISTORY)
                opponent = next(tag for tag in tags if tag != self.tags[code])
                members = {(tags[member["side"]], member["player_tag"]): {
                    "name": member["name"], "townhall": member["townhall"], "position": member["source_map_position"],
                    "expected": member["attacks_expected"], "used": member["attacks_used"],
                    "opponent_attacks": member["opponent_attacks"], "extra": json.loads(member["extra_json"]),
                    "rows": []} for member in war["members"]}
                attacks = {attack["attack_order"]: {
                    "attacker": attack["attacker"], "defender": attack["defender"], "stars": attack["stars"],
                    "destruction": attack["destruction"], "duration": attack["duration"],
                    "stub": json.loads(attack["extra_json"])["stub"], "extra": json.loads(attack["extra_json"]),
                    "rows": []} for attack in war["attacks"]}
                self.observations.append({
                    "kind": "payload", "code": code, "tag": self.tags[code], "opponent": opponent, "pair": tuple(tags),
                    "prep": war["preparation_at"], "start": war["start_at"], "end": war["end_at"], "state": war["state"],
                    "team_size": war["team_size"], "apm": war["attacks_per_member"], "war": war,
                    "names": {side["clan_tag"]: side["name"] for side in war["sides"]}, "seen": war["end_at"],
                    "members": members, "attacks": attacks,
                    "aliases": [("war_cache", f"{raw['preparationStartTime']}-{raw['endTime']}-{opponent}", code, slot,
                                 war["state"], None)],
                    "rows": [(HISTORY, code)]})

    def events(self):
        """Group observations of one war: same pair and preparation, then late captures by pair and end."""
        parent = list(range(len(self.observations)))

        def find(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def join(first, second):
            parent[find(second)] = find(first)

        first_by_key = {}
        for index, observation in enumerate(self.observations):
            if observation["prep"]:
                join(first_by_key.setdefault((observation["pair"], observation["prep"]), index), index)
        loose = [index for index, o in enumerate(self.observations) if o["kind"] == "participation"]
        for index in loose:
            observation = self.observations[index]
            near = {find(other) for other, candidate in enumerate(self.observations)
                    if candidate["kind"] != "participation" and candidate["state"] == "warEnded"
                    and candidate["pair"] == observation["pair"]
                    and abs(candidate["end"] - observation["end"]) <= CAPTURE_WINDOW}
            if len(near) == 1:
                join(next(iter(near)), index)
            elif near:
                observation["ambiguous"] = True
        free = sorted((index for index in loose if find(index) == index and not self.observations[index].get("ambiguous")),
                      key=lambda index: (self.observations[index]["pair"], self.observations[index]["end"]))
        for before, after in zip(free, free[1:]):
            first, second = self.observations[before], self.observations[after]
            if first["pair"] == second["pair"] and second["end"] - first["end"] <= CAPTURE_WINDOW:
                join(before, after)
        groups = defaultdict(list)
        for index, observation in enumerate(self.observations):
            groups[find(index)].append(observation)
        events = []
        for observations in groups.values():
            # Newest state first, then fuller evidence: a full payload, a lineup table, bare participation.
            observations.sort(key=lambda o: (-STATES[o["state"]], KINDS.index(o["kind"]), -o["seen"], o["aliases"][0][1]))
            events.append({"observations": observations, "pair": observations[0]["pair"],
                           "ends": sorted({o["end"] for o in observations}),
                           "ambiguous": any(o.get("ambiguous") for o in observations)})
        return sorted(events, key=lambda event: (event["ends"][0], event["pair"], event["observations"][0]["aliases"][0][1]))
