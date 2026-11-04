"""All-or-nothing copy of the old regular-war data into the family record; league wars are ``wars_import_cwl``.

The sources are frozen read-only (see ``wars_import_plan``): the old clan
health tables ``wars``, ``war_roster_members``, ``war_attacks`` and
``war_activity``, and the war board history of ``war_cache.json``. Each
source row ends up imported, matched to what the record already has, or
counted with a reason; any bad row or count mismatch aborts the whole copy. Facts the record already has are never overwritten, and every
old ID stays as an alias of the record's war.
"""

from collections import Counter, defaultdict
from hashlib import sha256
import json

from elbow_helper.configuration.clans import CLANS
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from . import ranked_import, wars_import_cwl
from .legacy_links import ImportMismatch, digest
from .legacy_wars import map_legacy_war
from .link_state import receipt
from .projections import canonical_json
from .war_projection import war_type
from .wars import END_DRIFT
from .wars_import_plan import CAPTURE_WINDOW, HISTORY, STATES, Plan, snapshot_wars

TOPIC = "wars_regular"
COLLECTOR = "wars_regular_import"
# A war with no recorded start or preparation cannot be judged friendly or regular by its times.
UNCHECKED = "legacy_unchecked"
SIDE_COLUMNS = ("name", "level", "badge_json", "stars", "destruction", "attacks", "xp_earned")
MAX_LISTED = 300


def same(first, second, fields):
    """Equal wherever both sides know the value."""
    return all(first[field] is None or second[field] is None or first[field] == second[field] for field in fields)


class Importer:
    def __init__(self, connection, frozen, plan, ledger, run):
        self.connection, self.frozen, self.plan, self.ledger, self.run = connection, frozen, plan, ledger, run
        self.at = frozen.frozen_at
        self.stats = defaultdict(Counter)
        self.pairs, self.accounts = {}, {}
        self.created, self.aliases = {}, {}
        self.inserted_members, self.inserted_attacks = [], []
        self.matches, self.conflicts, self.ambiguous, self.repaired = [], [], [], []
        self.methods, self.kinds, self.repaired_states = Counter(), Counter(), Counter()
        self.checks = {}

    def note(self, rows, outcome):
        for table, code in rows:
            self.ledger[table, code, outcome] += 1

    # Matching to what the record already has -------------------------------------------------

    def pair_wars(self, pair):
        if pair not in self.pairs:
            self.pairs[pair] = [dict(row) for row in self.connection.execute(
                "SELECT w.id,w.end_at,w.preparation_at FROM wars w JOIN war_sides a ON a.war_id=w.id AND a.side=0 "
                "AND a.clan_tag=? JOIN war_sides b ON b.war_id=w.id AND b.side=1 AND b.clan_tag=? "
                "WHERE w.war_tag IS NULL", pair)]
        return self.pairs[pair]

    def strong(self, event):
        """The old ID, preparation or end that says which record war this is; None when unknown or unclear."""
        found = {row[0] for o in event["observations"] for alias in o["aliases"] for row in self.connection.execute(
            "SELECT war_id FROM war_legacy_ids WHERE source=? AND legacy_id=? AND clan_code=?", alias[:3])}
        if len(found) > 1:
            raise ImportMismatch("legacy_alias_conflict", self.checks)
        if found:
            return next(iter(found)), "alias"
        prep = next((o["prep"] for o in event["observations"] if o["prep"]), None)
        if prep:
            row = self.connection.execute("SELECT war_id FROM war_source_keys WHERE source_kind='preparation' "
                                          "AND source_key=?", (":".join(event["pair"]) + f":{prep}",)).fetchone()
            if row:
                return row[0], "preparation"
        near = self.candidates(event, END_DRIFT)
        return (near[0], "end") if len(near) == 1 else (None, "ambiguous") if near else (None, None)

    def candidates(self, event, margin):
        prep = next((o["prep"] for o in event["observations"] if o["prep"]), None)
        return sorted({war["id"] for war in self.pair_wars(event["pair"])
                       if prep is None or war["preparation_at"] in (None, prep)
                       if any(abs(war["end_at"] - end) <= margin for end in event["ends"])})

    def resolve(self, events):
        """Decide every event's record war before writing: unique claims only, the rest stay apart and flagged."""
        decisions = [list(self.strong(event)) for event in events]
        for kind in ("preparation", "end"):
            claims = Counter(war for war, method in decisions if method == kind)
            for decision in decisions:
                if decision[1] == kind and claims[decision[0]] > 1:
                    decision[:] = [None, "ambiguous"]
        claimed = {war for war, method in decisions if war is not None}
        later = Counter()
        windows = {}
        for index, (event, decision) in enumerate(zip(events, decisions)):
            if decision[1] is None:
                windows[index] = [war for war in self.candidates(event, CAPTURE_WINDOW) if war not in claimed]
                later.update(windows[index])
        for index, wars in windows.items():
            if len(wars) == 1 and later[wars[0]] == 1:
                decisions[index] = [wars[0], "pause_window"]
            elif wars:
                decisions[index] = [None, "ambiguous"]
        return decisions

    # Writing -------------------------------------------------------------------------------

    def first(self, event, key):
        return next((o[key] for o in event["observations"] if o[key] is not None), None)

    def merged_members(self, event, finals=False):
        merged = {}
        for o in event["observations"]:
            if o["kind"] != "payload" and (not finals or o["state"] == "warEnded"):
                for key, entry in o["members"].items():
                    merged.setdefault(key, (o, entry))
        return merged

    def side_facts(self, event, tag):
        values, extra, best = dict.fromkeys(SIDE_COLUMNS), None, event["observations"][0]["state"]
        for o in event["observations"]:
            if o["kind"] == "payload" and o["state"] == best:
                side = next(s for s in o["war"]["sides"] if s["clan_tag"] == tag)
                for key in values:
                    values[key] = side[key] if values[key] is None else values[key]
                extra = extra or side["extra_json"]
            values["name"] = o["names"].get(tag) if values["name"] is None else values["name"]
        derived = []
        if values["attacks"] is None and best == "warEnded":
            used = [entry["used"] for (side_tag, _), (_, entry) in self.merged_members(event, finals=True).items()
                    if side_tag == tag]
            if used:
                values["attacks"], derived = sum(used), ["attacks"]
        return values, extra, derived

    def create(self, event, ambiguous):
        c, best, at = self.connection, event["observations"][0], self.at
        payload = best["war"] if best["kind"] == "payload" else None
        prep, start, team, apm = (self.first(event, key) for key in ("prep", "start", "team_size", "apm"))
        # A team size counted from an activity lineup is the copy's own finding, not a field the old data had.
        derived_team = next((o for o in event["observations"] if o["team_size"] is not None), {}).get("team_derived")
        kind, basis = (payload["type"], payload["type_basis"]) if payload else (
            war_type(prep, start, best["end"]) if prep and start else ("REG", UNCHECKED))
        presence = payload["field_presence_json"] if payload else canonical_json(sorted(
            name for name, value in (("attacksPerMember", apm), ("endTime", best["end"]), ("preparationStartTime", prep),
                                     ("startTime", start), ("state", best["state"]),
                                     ("teamSize", None if derived_team else team))
            if value is not None))
        values = {
            "type": kind, "type_basis": basis, "war_tag": None, "preparation_at": prep, "start_at": start,
            "war_start_at": payload["war_start_at"] if payload else None, "end_at": best["end"], "state": best["state"],
            "team_size": team, "attacks_per_member": apm,
            "battle_modifier_json": payload["battle_modifier_json"] if payload else None,
            "first_seen_at": min(min(o["seen"] for o in event["observations"]), at),
            "last_seen_at": min(max(o["seen"] for o in event["observations"]), at),
            "final_at": at if payload and payload["state"] == "warEnded" and payload["result_complete"] else None,
            "roster_complete": int(bool(payload and payload["roster_complete"])),
            "attacks_complete": int(bool(payload and payload["attacks_complete"])),
            "result_complete": int(bool(payload and payload["result_complete"])), "privacy_redacted": 0,
            "field_presence_json": presence}
        values["content_hash"] = sha256(canonical_json(values).encode("utf-8")).hexdigest()
        values["source_run_id"] = self.run
        war_id = c.execute(f"INSERT INTO wars({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                           tuple(values.values())).lastrowid
        for side, tag in enumerate(event["pair"]):
            facts, extra, derived = self.side_facts(event, tag)
            derived = derived + ["team_size"] if derived_team else derived
            extra = extra or canonical_json({"field_presence": ["name"] if facts["name"] else [], "legacy": True,
                                             **({"derived": derived} if derived else {})})
            c.execute("INSERT INTO war_sides(war_id,side,clan_tag,name,level,badge_json,stars,destruction,attacks,"
                      "xp_earned,extra_json,source_run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (war_id, side, tag, *(facts[key] for key in SIDE_COLUMNS), extra, self.run))
        self.keys(war_id, event, prep)
        if ambiguous:
            c.execute("INSERT OR IGNORE INTO war_source_keys VALUES ('legacy_ambiguous',?,?)",
                      (":".join(event["pair"]) + f":{best['end']}", war_id))
        self.created[war_id] = values
        return war_id

    def keys(self, war_id, event, prep):
        pair = ":".join(event["pair"])
        if prep:
            self.connection.execute("INSERT OR IGNORE INTO war_source_keys VALUES ('preparation',?,?)",
                                    (f"{pair}:{prep}", war_id))
        self.connection.execute("INSERT OR IGNORE INTO war_source_keys VALUES ('end',?,?)",
                                (f"{pair}:{event['observations'][0]['end']}", war_id))

    def fill(self, war_id, event):
        """Add only what the record lacks; a record value that differs stays and is listed."""
        c = self.connection
        war = c.execute("SELECT * FROM wars WHERE id=?", (war_id,)).fetchone()
        changes = {}
        for column, key in (("preparation_at", "prep"), ("start_at", "start"), ("team_size", "team_size"),
                            ("attacks_per_member", "apm")):
            value = self.first(event, key)
            if value is None:
                continue
            if war[column] is None:
                changes[column] = value
            elif war[column] != value and len(self.conflicts) < MAX_LISTED:
                self.conflicts.append({"war_id": war_id, "field": column, "record": war[column], "legacy": value})
        best = event["observations"][0]
        # A war an earlier import wrote and nothing collected since may take a later old capture's state.
        writer = c.execute("SELECT collector FROM collection_runs WHERE id=?", (war["source_run_id"],)).fetchone()
        if writer and writer[0] == COLLECTOR and STATES[best["state"]] > STATES[war["state"]]:
            changes.update(state=best["state"], last_seen_at=min(max(o["seen"] for o in event["observations"]), self.at))
        if changes:
            c.execute(f"UPDATE wars SET {','.join(f'{key}=?' for key in changes)},source_run_id=? WHERE id=?",
                      (*changes.values(), self.run, war_id))
        if "preparation_at" in changes:
            self.keys(war_id, event, changes["preparation_at"])
        sides = {row["clan_tag"]: row["side"] for row in c.execute("SELECT side,clan_tag FROM war_sides WHERE war_id=?",
                                                                   (war_id,))}
        for tag, side in sides.items():
            facts = self.side_facts(event, tag)[0]
            for column in SIDE_COLUMNS:
                if facts[column] is not None:
                    c.execute(f"UPDATE war_sides SET {column}=?,source_run_id=? WHERE war_id=? AND side=? AND {column} IS NULL",
                              (facts[column], self.run, war_id, side))

    def account(self, tag):
        if tag not in self.accounts:
            row = self.connection.execute("SELECT id FROM accounts WHERE player_tag=?", (tag,)).fetchone()
            self.accounts[tag] = row[0] if row else None
        return self.accounts[tag]

    def versus(self, winner, other, fields):
        """What becomes of an old capture's row when a better capture of the same war has its own."""
        if same(winner[1], other[1], fields):
            # The same fact under another old ID or capture of this war.
            self.stats[other[0]["code"]]["duplicate_rows"] += len(other[1]["rows"])
            return "matched"
        older = STATES[other[0]["state"]] < STATES[winner[0]["state"]] or (
            other[0].get("repaired") and not winner[0].get("repaired"))
        return "excluded:older_capture" if older else "conflict"

    def add_members(self, war_id, event):
        c = self.connection
        winners = {}
        for o in event["observations"]:
            for key, entry in o["members"].items():
                if key[0] not in event["pair"]:
                    raise ImportMismatch("lineup_side_mismatch", self.checks)
                if key not in winners:
                    winners[key] = (o, entry)
                else:
                    self.note(entry["rows"], self.versus(winners[key], (o, entry), ("townhall", "expected", "used")))
        # A lineup the collector wrote stays as it is; rows an earlier import wrote may still be completed.
        protected = {tag: c.execute(
            "SELECT 1 FROM war_members m JOIN collection_runs r ON r.id=m.source_run_id "
            "WHERE m.war_id=? AND m.side=? AND r.collector!=?", (war_id, side, COLLECTOR)).fetchone() is not None
            for side, tag in enumerate(event["pair"])}
        for (tag, player), (o, entry) in sorted(winners.items()):
            side = event["pair"].index(tag)
            old = c.execute("SELECT townhall,attacks_expected,attacks_used FROM war_members "
                            "WHERE war_id=? AND side=? AND player_tag=?", (war_id, side, player)).fetchone()
            if old is not None:
                stored = {"townhall": old[0], "expected": old[1], "used": old[2]}
                agrees = same(stored, entry, tuple(stored))
                self.note(entry["rows"], "matched" if agrees else "conflict")
                if not agrees and len(self.conflicts) < MAX_LISTED:
                    self.conflicts.append({"war_id": war_id, "player_tag": player, "record": stored,
                                           "legacy": {key: entry[key] for key in stored}})
            elif protected[tag]:
                self.note(entry["rows"], "kept_record")
            else:
                account = self.account(player)
                c.execute("INSERT INTO war_members(war_id,side,player_tag,account_id,name,townhall,source_map_position,"
                          "attacks_expected,attacks_used,opponent_attacks,extra_json,source_run_id) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (war_id, side, player, account, entry["name"], entry["townhall"], entry["position"],
                           entry["expected"], entry["used"], entry.get("opponent_attacks"),
                           canonical_json(entry["extra"]), self.run))
                self.note(entry["rows"], "imported")
                self.inserted_members.append((war_id, side, player, entry["expected"], entry["used"]))
                self.stats[o["code"]]["lineup_rows"] += 1
                self.stats[o["code"]]["unlinked_accounts"] += account is None

    def add_attacks(self, war_id, event):
        c = self.connection
        winners = {}
        for o in event["observations"]:
            for order, entry in o["attacks"].items():
                previous = winners.get(order)
                if previous is None or previous[1]["stub"] and not entry["stub"]:
                    # A stub of the best opponent attack carries no rows; a real attack replaces it.
                    winners[order] = (o, entry)
                else:
                    self.note(entry["rows"], self.versus(previous, (o, entry), (
                        "attacker", "defender", "stars", "destruction")))
        ids = defaultdict(list)
        for row in c.execute("SELECT id,player_tag FROM war_members WHERE war_id=?", (war_id,)):
            ids[row["player_tag"]].append(row["id"])

        def member(tag):
            return ids[tag][0] if len(ids.get(tag, ())) == 1 else None

        protected = c.execute(
            "SELECT 1 FROM war_attacks a JOIN collection_runs r ON r.id=a.source_run_id WHERE a.war_id=? AND "
            "COALESCE(json_extract(a.extra_json,'$.stub'),0)=0 AND r.collector!=?", (war_id, COLLECTOR)).fetchone()
        for order, (o, entry) in sorted(winners.items()):
            old = c.execute("SELECT * FROM war_attacks WHERE war_id=? AND attack_order=?", (war_id, order)).fetchone()
            if old is not None:
                extra = json.loads(old["extra_json"])
                tags = [self.member_tag(old[column]) or extra.get(name) for column, name in (
                    ("attacker_member_id", "attacker_tag"), ("defender_member_id", "defender_tag"))]
                agrees = tags == [entry["attacker"], entry["defender"]]
                self.note(entry["rows"], "matched" if agrees else "conflict")
                if not agrees and len(self.conflicts) < MAX_LISTED:
                    self.conflicts.append({"war_id": war_id, "attack_order": order, "record": tags,
                                           "legacy": [entry["attacker"], entry["defender"]]})
            elif protected:
                self.note(entry["rows"], "kept_record")
            else:
                attacker, defender = member(entry["attacker"]), member(entry["defender"])
                extra = dict(entry["extra"])
                if attacker is None or defender is None:
                    extra.update(attacker_tag=entry["attacker"], defender_tag=entry["defender"])
                c.execute("INSERT INTO war_attacks(war_id,attack_order,attacker_member_id,defender_member_id,stars,"
                          "destruction,duration,extra_json,source_run_id) VALUES (?,?,?,?,?,?,?,?,?)",
                          (war_id, order, attacker, defender, entry["stars"], entry["destruction"], entry["duration"],
                           canonical_json(extra), self.run))
                self.note(entry["rows"], "imported")
                self.inserted_attacks.append((war_id, order, entry["stars"], entry["destruction"]))
                self.stats[o["code"]]["attacks"] += 1

    def member_tag(self, member_id):
        row = self.connection.execute("SELECT player_tag FROM war_members WHERE id=?", (member_id,)).fetchone() \
            if member_id is not None else None
        return row[0] if row else None

    def alias(self, war_id, alias):
        source, legacy_id, code, label, state, captured = alias
        evidence = {"label": label, "capture_state": state, "captured_at": captured}
        try:
            added = map_legacy_war(self.connection, source, legacy_id, code, war_id, **evidence, at=self.at)
        except ValueError as error:
            old = self.connection.execute(
                "SELECT war_id,capture_state,captured_at FROM war_legacy_ids WHERE source=? AND legacy_id=? AND clan_code=?",
                (source, legacy_id, code)).fetchone()
            # A later capture of the same old war replaces what its alias said; anything else is a conflict.
            if (str(error) != "legacy_war_conflict" or old is None or old["war_id"] != war_id
                    or (captured or 0) < (old["captured_at"] or 0)
                    or STATES.get(state, -1) < STATES.get(old["capture_state"], -1)):
                raise ImportMismatch(str(error), self.checks) from error
            self.connection.execute("UPDATE war_legacy_ids SET legacy_label=?,capture_state=?,captured_at=?,mapped_at=? "
                                    "WHERE source=? AND legacy_id=? AND clan_code=?",
                                    (label, state, captured, self.at, source, legacy_id, code))
            added = None
        self.aliases[source, legacy_id, code] = war_id
        self.stats[code]["aliases"] += 1
        self.stats[code]["aliases_added"] += bool(added)

    def write(self, events):
        decisions = self.resolve(events)
        for event, (match, method) in zip(events, decisions):
            ambiguous = method == "ambiguous" or event["ambiguous"]
            if match is None:
                war_id = self.create(event, ambiguous)
            else:
                war_id = match
                sides = [row[0] for row in self.connection.execute(
                    "SELECT clan_tag FROM war_sides WHERE war_id=? ORDER BY side", (war_id,))]
                if tuple(sides) != event["pair"]:
                    raise ImportMismatch("war_pair_mismatch", self.checks)
                self.fill(war_id, event)
                self.methods[method] += 1
                if len(self.matches) < MAX_LISTED:
                    self.matches.append({"war_id": war_id, "method": method, "legacy": [
                        [alias[0], alias[1], alias[2]] for o in event["observations"] for alias in o["aliases"]]})
            if ambiguous and len(self.ambiguous) < MAX_LISTED:
                self.ambiguous.append({"war_id": war_id, "legacy": [
                    [alias[0], alias[1], alias[2]] for o in event["observations"] for alias in o["aliases"]]})
            self.add_members(war_id, event)
            self.add_attacks(war_id, event)
            for o in event["observations"]:
                for alias in o["aliases"]:
                    self.alias(war_id, alias)
                self.note(o["rows"], "imported" if match is None else "matched")
            for code in {o["code"] for o in event["observations"]}:
                self.stats[code]["unique_wars"] += 1
            # Several old IDs of one clan for one war: pauses, preparation captures and repeated captures.
            for code, n in Counter(alias[2] for o in event["observations"] for alias in o["aliases"]
                                   if alias[0] == "clan_health").items():
                self.stats[code]["merged_ids"] += n - 1
            row = self.connection.execute("SELECT type,type_basis,state FROM wars WHERE id=?", (war_id,)).fetchone()
            self.kinds["/".join(row)] += 1
            repaired = [alias[1] for o in event["observations"] if o.get("repaired") for alias in o["aliases"]]
            if repaired:
                self.repaired_states[row["state"]] += 1
                if len(self.repaired) < MAX_LISTED:
                    self.repaired.append({"war_id": war_id, "state": row["state"], "created": match is None,
                                          "legacy": repaired})

    # Checks --------------------------------------------------------------------------------

    def verify(self):
        c = self.connection
        for table, rows in self.frozen.rows.items():
            counted = sum(n for (name, _, _), n in self.ledger.items() if name == table)
            if counted != len(rows):
                raise ImportMismatch(f"source_rows_unaccounted:{table}", self.checks)
        slots = 2 * len(self.frozen.history)
        if sum(n for (name, _, _), n in self.ledger.items() if name == HISTORY) != slots:
            raise ImportMismatch("source_rows_unaccounted:" + HISTORY, self.checks)
        for (source, legacy_id, code), war_id in self.aliases.items():
            row = c.execute("SELECT war_id FROM war_legacy_ids WHERE source=? AND legacy_id=? AND clan_code=?",
                            (source, legacy_id, code)).fetchone()
            if row is None or row[0] != war_id:
                raise ImportMismatch("alias_missing", self.checks)
        # Each inserted lineup row and attack must read back as planned; a war made here holds nothing else.
        members, attacks = defaultdict(dict), defaultdict(dict)
        for war_id, side, player, expected, used in self.inserted_members:
            members[war_id][side, player] = (expected, used)
        for war_id, order, stars, destruction in self.inserted_attacks:
            attacks[war_id][order] = (stars, destruction)
        for war_id in members.keys() | attacks.keys() | self.created.keys():
            stored = {(r[0], r[1]): (r[2], r[3]) for r in c.execute(
                "SELECT side,player_tag,attacks_expected,attacks_used FROM war_members WHERE war_id=?", (war_id,))}
            if any(stored.get(key) != value for key, value in members[war_id].items()) or (
                    war_id in self.created and set(stored) != set(members[war_id])):
                raise ImportMismatch("lineup_mismatch", self.checks)
            stored = {r[0]: (r[1], r[2]) for r in c.execute(
                "SELECT attack_order,stars,destruction FROM war_attacks WHERE war_id=?", (war_id,))}
            if any(stored.get(key) != value for key, value in attacks[war_id].items()) or (
                    war_id in self.created and set(stored) != set(attacks[war_id])):
                raise ImportMismatch("attack_mismatch", self.checks)
        for war_id, values in self.created.items():
            row = c.execute("SELECT preparation_at,end_at,team_size,attacks_per_member,state FROM wars WHERE id=?",
                            (war_id,)).fetchone()
            if tuple(row) != tuple(values[key] for key in ("preparation_at", "end_at", "team_size", "attacks_per_member",
                                                          "state")):
                raise ImportMismatch("war_mismatch", self.checks)
        if c.execute("PRAGMA foreign_key_check").fetchone():
            raise ImportMismatch("family_foreign_key_check", self.checks)

    def report(self, events):
        outcomes = defaultdict(Counter)
        reasons = defaultdict(lambda: defaultdict(Counter))
        for (table, code, outcome), n in self.ledger.items():
            outcomes[table][outcome] += n
            if outcome.startswith("excluded:"):
                reasons[code][table][outcome[9:]] += n
        clans = {}
        for code in sorted({code for (_, code, _) in self.ledger} | set(self.stats) | set(self.plan.old_wars)):
            clans[code] = {"old_wars": len(self.plan.old_wars.get(code, ())),
                           **{key: self.stats[code][key] for key in (
                               "unique_wars", "aliases", "aliases_added", "merged_ids", "duplicate_rows", "lineup_rows",
                               "attacks", "unlinked_accounts")},
                           "stuck_wars": dict(sorted(self.plan.repairs[code].items())),
                           "preparation_only_ids": dict(sorted(self.plan.preparation_ids[code].items())),
                           "activity_lineups": dict(sorted(self.plan.lineups[code].items())),
                           "excluded": {table: dict(sorted(counts.items())) for table, counts in reasons[code].items()}}
        return {"source_rows": {table: len(rows) for table, rows in self.frozen.rows.items()} | {
                    HISTORY: 2 * len(self.frozen.history)},
                "outcomes": {table: dict(sorted(counts.items())) for table, counts in sorted(outcomes.items())},
                "clans": clans,
                "wars": {"events": len(events), "created": len(events) - sum(self.methods.values()),
                         "matched_existing": dict(sorted(self.methods.items())), "by_type_state": dict(sorted(self.kinds.items())),
                         "ambiguous": len(self.ambiguous), "ended_after_last_check": dict(sorted(self.repaired_states.items()))},
                "inserted": {"lineup_rows": len(self.inserted_members), "attacks": len(self.inserted_attacks)},
                "matched_existing": self.matches, "record_conflicts": self.conflicts, "ambiguous": self.ambiguous,
                "ended_after_last_check": self.repaired}


def copy_at_startup(connection, health_path, cache_path, ranked_path=None, *, clans=None):
    """The once-only startup copy of wars and ranked battles; done topics never read old files.

    Parts share frozen source reads and one transaction: a failure in any keeps nothing. Returns each part's
    result, None for a part an earlier start already copied. Wars the old files gain after the copy are not
    added: the collector owns every war from then on.
    """
    topics = {TOPIC, wars_import_cwl.TOPIC} | ({ranked_import.TOPIC} if ranked_path is not None else set())
    done = {row[0] for row in connection.execute("SELECT topic FROM imports") if row[0] in topics}
    if done == topics:
        return None
    frozen = snapshot_wars(health_path, cache_path) if {TOPIC, wars_import_cwl.TOPIC} - done else None
    ranked = ranked_import.snapshot_ranked(ranked_path) if ranked_import.TOPIC in topics - done else None
    clans = {code: clan.tag for code, clan in CLANS.items()} if clans is None else clans
    with sqlite_transaction(connection, immediate=True):
        ranked_plan = ranked_import.plan_ranked(connection, ranked) if ranked is not None else None
        regular = None if TOPIC in done else write_regular(connection, frozen, clans)
        league = None if wars_import_cwl.TOPIC in done else wars_import_cwl.write_cwl(connection, frozen, clans)
        battles = ranked_import.write_ranked(connection, ranked, ranked_plan) if ranked is not None else None
    return {"regular": regular, "cwl": league, **({"ranked": battles} if ranked_path is not None else {})}


def import_wars(connection, health_path, cache_path=None, frozen=None, *, clans=None):
    """Caller holds the family writer. The old bot may keep writing: the copy is of one consistent snapshot.

    ``clans`` maps clan codes to tags (the family's nine by default). A repeat
    with unchanged sources adds nothing; changed sources are reconciled by the
    same checked copy, which only adds what the record lacks.
    """
    frozen = snapshot_wars(health_path, cache_path) if frozen is None else frozen
    clans = {code: clan.tag for code, clan in CLANS.items()} if clans is None else clans
    with sqlite_transaction(connection, immediate=True):
        return write_regular(connection, frozen, clans)


def write_regular(connection, frozen, clans):
    """The regular-war part of the copy, inside the caller's transaction; returns its result, marker written."""
    ledger = Counter()
    plan = Plan(frozen, clans, ledger)
    events = plan.events()
    marker = connection.execute("SELECT * FROM imports WHERE topic=?", (TOPIC,)).fetchone()
    if marker is not None and marker["source_digest"] == frozen.digest:
        return {"imported": False, "source_version": frozen.version, "source_digest": frozen.digest,
                "frozen_at": marker["frozen_at"], "checks": json.loads(marker["checks_json"])}
    item = receipt(connection, COLLECTOR, TOPIC, frozen.frozen_at, len(events))
    run = connection.execute("SELECT run_id FROM collection_items WHERE id=?", (item,)).fetchone()[0]
    importer = Importer(connection, frozen, plan, ledger, run)
    importer.write(events)
    importer.verify()
    checks = importer.checks = importer.report(events)
    written = len(importer.inserted_members) + len(importer.inserted_attacks)
    connection.execute("UPDATE collection_runs SET rows_written=? WHERE id=?", (written, run))
    connection.execute("UPDATE collection_items SET written_count=? WHERE id=?", (written, item))
    mapping = digest(sorted([*key, war_id] for key, war_id in importer.aliases.items()))
    connection.execute(
        "INSERT INTO imports VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(topic) DO UPDATE SET "
        "source_version=excluded.source_version,source_digest=excluded.source_digest,frozen_at=excluded.frozen_at,"
        "current_links_digest=excluded.current_links_digest,checked_at=excluded.checked_at,"
        "checks_json=excluded.checks_json,source_item_id=excluded.source_item_id",
        (TOPIC, frozen.version, frozen.digest, frozen.frozen_at, mapping, frozen.frozen_at,
         canonical_json(checks), item))
    return {"imported": True, "source_version": frozen.version, "source_digest": frozen.digest,
            "frozen_at": frozen.frozen_at, "checks": checks}
