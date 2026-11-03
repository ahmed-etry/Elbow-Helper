"""All-or-nothing copy of the old CWL wars into the family record.

It works from the regular copy's frozen snapshot of the old clan health tables
and runs in the same startup transaction. A league week the collector owns (a
war ending on or after ``CUTOFF``, or not ended when the snapshot was taken) is
left out whole. Each copied week becomes a closed season: its group, its clans,
the family clan's players, one round per war, and each war's family side from
its final lineup. The opponent side holds only the bases that were hit, flagged
partial, so nothing reads a zero or a miss from it. Every old row is copied,
matched or counted with its reason; anything that does not add up aborts the copy.
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from .legacy_links import ImportMismatch, digest
from .legacy_wars import map_legacy_war
from .link_state import receipt
from .projections import canonical_json
from .wars import SECOND
from .wars_import_plan import STATES, bad, count, number, snapshot_wars, text

TOPIC = "wars_cwl"
COLLECTOR = "wars_cwl_import"
# The collector saves every league week from November 2026 on, so the copy never touches a running season.
CUTOFF = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()) * SECOND
# The war log's league-week entry ends with the week's last war, give or take.
LOG_WINDOW = 24 * 3600 * SECOND
# Each war a clan wins adds this many stars to its league-week total in the war log.
WIN_BONUS = 10
CATCHUP = "-catchup"
TABLES = ("wars", "war_roster_members", "war_attacks", "war_activity")
WAR_FIELDS = ["attacksPerMember", "endTime", "preparationStartTime", "startTime", "state", "teamSize"]
MEMBER_FIELDS = ["mapPosition", "name", "tag", "townhallLevel"]
ATTACK_FIELDS = ["attackerTag", "defenderTag", "destructionPercentage", "duration", "order", "stars"]
MAX_LISTED = 300


def season_key_valid(key):
    """Old season keys are a month, or a month with -catchup for that month's second league."""
    if not isinstance(key, str):
        return False
    month = key[:-len(CATCHUP)] if key.endswith(CATCHUP) else key
    try:
        return datetime.strptime(month, "%Y-%m").strftime("%Y-%m") == month
    except ValueError:
        return False


def position_basis(positions, size):
    """Lineup positions run 1..team size; the oldest weeks kept each player's place in the league roster."""
    if any(position is None for position in positions):
        return "unknown"
    return "lineup" if sorted(positions) == list(range(1, size + 1)) else "league_roster"


class CwlPlan:
    """The old CWL rows, checked and grouped into league weeks; nothing here touches the record."""

    def __init__(self, frozen, clans, ledger):
        self.frozen, self.ledger = frozen, ledger
        self.tags = {code: normalize_player_tag(tag) for code, tag in clans.items()}
        self.family = set(self.tags.values())
        self.source_rows = Counter()
        self.wars, self.left_out = {}, []
        self.read_wars()
        self.read_rows()
        self.groups = self.build_groups()

    def count(self, table, code, season, outcome, n=1):
        self.ledger[table, code, season, outcome] += n

    def read_wars(self):
        tags = {}
        for row in self.frozen.rows["wars"]:
            if row["war_type"] != "CWL":
                continue
            self.source_rows["wars"] += 1
            code = row["clan_code"]
            if code not in self.tags or normalize_player_tag(row["clan_tag"]) != self.tags[code]:
                bad("wars")
            old_id = row["war_id"]
            tag = normalize_player_tag(old_id[4:]) if text(old_id) and old_id.startswith("CWL:") else None
            opponent = normalize_player_tag(row["opponent_tag"])
            times = ("preparation_start_ts", "start_ts", "end_ts", "last_seen_ts")
            if (not tag or not opponent or opponent == self.tags[code] or not text(row["opponent_name"])
                    or row["state"] not in STATES or not season_key_valid(row["cwl_season"])
                    or not all(text(row[key]) for key in ("cwl_season_label", "cwl_league", "source"))
                    or not all(count(row[key], 1) for key in ("cwl_round", "team_size", "attacks_per_member", *times))
                    or not row["preparation_start_ts"] <= row["start_ts"] <= row["end_ts"]):
                bad("wars")
            if opponent in self.family:
                raise ImportMismatch("cwl_family_opponent")
            if tag in tags:
                raise ImportMismatch("cwl_war_tag_shared")
            tags[tag] = code
            self.wars[code, old_id] = {
                "code": code, "old_id": old_id, "tag": tag, "clan": self.tags[code], "opponent": opponent,
                "opponent_name": row["opponent_name"] or None, "season": row["cwl_season"],
                "label": row["cwl_season_label"] or None, "league": row["cwl_league"] or None, "round": row["cwl_round"],
                "team_size": row["team_size"], "apm": row["attacks_per_member"], "state": row["state"],
                "prep": row["preparation_start_ts"] * SECOND, "start": row["start_ts"] * SECOND,
                "end": row["end_ts"] * SECOND, "seen": row["last_seen_ts"] * SECOND, "source": row["source"],
                "roster": {}, "attacks": [], "activity": []}

    def read_rows(self):
        rows = self.frozen.rows
        regular = {(row["clan_code"], row["war_id"]) for row in rows["wars"] if row["war_type"] != "CWL"}
        for row in rows["war_roster_members"]:
            key = (row["clan_code"], row["war_id"])
            # The regular copy counts the same rows out as league rows.
            if key not in self.wars and (key in regular or not str(row["war_id"]).startswith("CWL:")):
                continue
            self.source_rows["war_roster_members"] += 1
            tag = normalize_player_tag(row["player_tag"])
            if (not tag or not text(row["player_name"]) or row["roster_state"] not in STATES or not text(row["source"])
                    or not all(count(row[field]) for field in ("townhall", "map_position", "attacks_expected",
                                                               "attacks_used", "captured_ts"))):
                bad("war_roster_members")
            if key not in self.wars:
                self.count("war_roster_members", row["clan_code"], "", "excluded:no_war_row")
            else:
                self.wars[key]["roster"][tag] = row
        for row in rows["war_attacks"]:
            if row["war_type"] != "CWL":
                continue
            self.source_rows["war_attacks"] += 1
            key = (row["clan_code"], row["war_id"])
            if (not normalize_player_tag(row["player_tag"]) or not normalize_player_tag(row["defender_tag"])
                    or row["war_state"] not in STATES or not count(row["attack_order"], 1)
                    or type(row["stars"]) is not int or not 0 <= row["stars"] <= 3
                    or not number(row["destruction"], 100) or not count(row["duration"])
                    or not all(count(row[field]) for field in ("defender_map_position", "defender_townhall", "end_ts"))
                    or row["fresh_attack"] not in (0, 1) or not text(row["defender_name"]) or not text(row["source"])):
                bad("war_attacks")
            if key not in self.wars:
                self.count("war_attacks", row["clan_code"], "", "excluded:no_war_row")
            else:
                self.wars[key]["attacks"].append(row)
        for row in rows["war_activity"]:
            if row["war_type"] != "CWL":
                continue
            self.source_rows["war_activity"] += 1
            key = (row["clan_code"], row["war_id"])
            if (not normalize_player_tag(row["player_tag"]) or not text(row["player_name"]) or not text(row["source"])
                    or not all(count(row[field]) for field in ("attacks_expected", "attacks_used", "attack_count"))):
                bad("war_activity")
            if key not in self.wars:
                self.count("war_activity", row["clan_code"], "", "excluded:no_war_row")
            else:
                self.wars[key]["activity"].append(row)

    def exclude(self, wars, reason):
        for war in wars:
            for table, n in (("wars", 1), ("war_roster_members", len(war["roster"])),
                             ("war_attacks", len(war["attacks"])), ("war_activity", len(war["activity"]))):
                if n:
                    self.count(table, war["code"], war["season"], "excluded:" + reason, n)

    def reason_left_out(self, wars):
        """Why a league week is not copied; None when it is a whole, ended week."""
        if any(war["end"] >= CUTOFF for war in wars):
            return "collector_season"
        if any(war["end"] > self.frozen.frozen_at or war["state"] != "warEnded" for war in wars):
            return "not_ended"
        opponents = {war["opponent"] for war in wars}
        # A group of n clans plays n-1 rounds, one war per clan each round.
        if ([war["round"] for war in wars] != list(range(1, len(wars) + 1)) or len(opponents) != len(wars)
                or len(wars) % 2 == 0 or any(a["end"] >= b["end"] for a, b in zip(wars, wars[1:]))
                or len({(war["label"], war["league"]) for war in wars}) != 1):
            return "incomplete_group"
        return None

    def build_groups(self):
        weeks = defaultdict(list)
        for war in self.wars.values():
            weeks[war["code"], war["season"]].append(war)
        groups = []
        for (code, season), wars in sorted(weeks.items()):
            wars.sort(key=lambda war: (war["round"], war["end"]))
            reason = self.reason_left_out(wars)
            if reason:
                self.exclude(wars, reason)
                self.left_out.append({"clan": code, "season_key": season, "reason": reason, "wars": len(wars)})
                continue
            for war in wars:
                self.check_war(war)
            clan = self.tags[code]
            groups.append({"code": code, "clan": clan, "season": season, "label": wars[0]["label"],
                           "league": wars[0]["league"], "sources": sorted({war["source"] for war in wars}),
                           "group_key": ":".join(sorted({clan, *(war["opponent"] for war in wars)})),
                           "first_seen": min(min(war["seen"] for war in wars), self.frozen.frozen_at), "wars": wars})
        return sorted(groups, key=lambda group: (group["wars"][0]["end"], group["code"]))

    def check_war(self, war):
        """The final lineup decides: every attack needs its attacker in it, and each player's attacks add up."""
        roster, code, season = war["roster"], war["code"], war["season"]
        if not roster:
            raise ImportMismatch("cwl_lineup_missing")
        orders, used, defenders, best = set(), Counter(), {}, {}
        for row in war["attacks"]:
            attacker, defender = normalize_player_tag(row["player_tag"]), normalize_player_tag(row["defender_tag"])
            if attacker not in roster:
                raise ImportMismatch("cwl_attacker_without_roster")
            if defender in roster:
                raise ImportMismatch("cwl_defender_on_own_side")
            if row["attack_order"] in orders:
                raise ImportMismatch("cwl_attack_order_repeated")
            orders.add(row["attack_order"])
            used[attacker] += 1
            facts = (row["defender_name"] or None, row["defender_townhall"] or None,
                     row["defender_map_position"] or None)
            if defenders.setdefault(defender, facts) != facts:
                raise ImportMismatch("cwl_defender_conflict")
            stars, destruction = best.get(defender, (0, 0))
            best[defender] = (max(stars, row["stars"]), max(destruction, row["destruction"]))
        if any(used[tag] != row["attacks_used"] for tag, row in roster.items()):
            raise ImportMismatch("cwl_attack_count_mismatch")
        war["conflicts"] = []
        for row in war["activity"]:
            entry = roster.get(normalize_player_tag(row["player_tag"]))
            if entry is None:
                # An early lineup capture of a player swapped out before battle day: never a missed attack.
                if row["attacks_used"]:
                    raise ImportMismatch("cwl_activity_outside_roster")
                self.count("war_activity", code, season, "excluded:swapped_out")
            elif (entry["attacks_expected"], entry["attacks_used"]) == (row["attacks_expected"], row["attacks_used"]):
                self.count("war_activity", code, season, "matched")
            else:
                self.count("war_activity", code, season, "conflict")
                war["conflicts"].append({"legacy_id": war["old_id"], "player_tag": entry["player_tag"],
                                         "roster": [entry["attacks_expected"], entry["attacks_used"]],
                                         "activity": [row["attacks_expected"], row["attacks_used"]]})
        own = position_basis([row["map_position"] or None for row in roster.values()], war["team_size"])
        hit = [facts[2] for facts in defenders.values()]
        other = ("unknown" if None in hit else "league_roster" if any(p > war["team_size"] for p in hit) else own)
        war["defenders"], war["basis"] = defenders, (own, other)
        # Clash scores a war by each base's best attack: stars summed, destruction averaged over the team.
        war["derived"] = {"stars": sum(stars for stars, _ in best.values()),
                          "destruction": sum(value for _, value in best.values()) / war["team_size"],
                          "attacks": sum(row["attacks_used"] for row in roster.values())}


class CwlImporter:
    def __init__(self, connection, frozen, plan, ledger, run):
        self.connection, self.frozen, self.plan, self.ledger, self.run = connection, frozen, plan, ledger, run
        self.at = frozen.frozen_at
        self.accounts, self.aliases, self.created, self.seasons = {}, {}, {}, {}
        self.kept, self.logs, self.stats = set(), {}, defaultdict(Counter)
        self.checks = {}

    def account(self, tag):
        if tag not in self.accounts:
            row = self.connection.execute("SELECT id FROM accounts WHERE player_tag=?", (tag,)).fetchone()
            self.accounts[tag] = row[0] if row else None
        return self.accounts[tag]

    def note_rows(self, war, outcome):
        for table, n in (("wars", 1), ("war_roster_members", len(war["roster"])), ("war_attacks", len(war["attacks"]))):
            if n:
                self.plan.count(table, war["code"], war["season"], outcome, n)

    # Matching to what the record already has -------------------------------------------------

    def in_record(self, group):
        """The record's wars for this week when it holds all of them as the old store does; None when it holds none."""
        c, present = self.connection, {}
        for war in group["wars"]:
            row = c.execute("SELECT * FROM wars WHERE war_tag=?", (war["tag"],)).fetchone()
            key = c.execute("SELECT war_id FROM war_source_keys WHERE source_kind='cwl' AND source_key=?",
                            (war["tag"],)).fetchone()
            if key is not None and (row is None or key[0] != row["id"]):
                raise ImportMismatch("cwl_record_conflict", self.checks)
            if row is None:
                continue
            sides = [r[0] for r in c.execute("SELECT clan_tag FROM war_sides WHERE war_id=? ORDER BY side", (row["id"],))]
            facts = {"preparation_at": war["prep"], "start_at": war["start"], "end_at": war["end"],
                     "team_size": war["team_size"], "attacks_per_member": war["apm"]}
            if (row["type"] != "CWL" or sides != sorted((war["clan"], war["opponent"]))
                    or any(row[key] is not None and row[key] != value for key, value in facts.items())):
                raise ImportMismatch("cwl_record_conflict", self.checks)
            present[war["tag"]] = row["id"]
        season = c.execute("SELECT id FROM cwl_seasons WHERE season_key=? AND group_key=?",
                           (group["season"], group["group_key"])).fetchone()
        if not present:
            if season is not None:
                raise ImportMismatch("cwl_record_conflict", self.checks)
            return None
        if len(present) != len(group["wars"]):
            raise ImportMismatch("cwl_group_partly_in_record", self.checks)
        return present

    # Writing -------------------------------------------------------------------------------

    def write(self):
        for group in self.plan.groups:
            present = self.in_record(group)
            if present is None:
                self.create_group(group)
            else:
                self.kept.add((group["code"], group["season"]))
                for war in group["wars"]:
                    war["war_id"] = present[war["tag"]]
                    self.note_rows(war, "kept_record")
            for war in group["wars"]:
                self.alias(war)

    def create_group(self, group):
        c, at, run, wars = self.connection, self.at, self.run, group["wars"]
        metadata = {"round_count": len(wars), "expected_round_count": len(wars), "wars_per_round": (len(wars) + 1) // 2,
                    "placeholder_rounds": [], "field_presence": [],
                    "legacy": {"season_key": group["season"], "season_label": group["label"], "league": group["league"],
                               "sources": group["sources"], "rounds_hold": "family_wars_only"}}
        season_id = c.execute(
            "INSERT INTO cwl_seasons(season_key,group_key,state,league_id,league_name,first_seen_at,final_at,"
            "group_metadata_json,source_run_id) VALUES (?,?,'ended',NULL,?,?,?,?,?)",
            (group["season"], group["group_key"], group["league"], group["first_seen"], at, canonical_json(metadata),
             run)).lastrowid
        self.seasons[season_id] = group
        c.execute("INSERT INTO cwl_season_sources VALUES (?,?)", (season_id, group["clan"]))
        names = {war["opponent"]: war["opponent_name"] for war in wars}
        for tag in sorted({group["clan"], *names}):
            extra = {"legacy": True, **({"partial": True} if tag != group["clan"] else {})}
            c.execute("INSERT INTO cwl_clans(season_id,clan_tag,name,level,badge_json,extra_json,source_run_id) "
                      "VALUES (?,?,?,NULL,NULL,?,?)", (season_id, tag, names.get(tag), canonical_json(extra), run))
        players = {}
        for war in wars:
            for tag, row in war["roster"].items():
                entry = players.setdefault(tag, {"wars": 0})
                entry.update(wars=entry["wars"] + 1, name=row["player_name"] or None, townhall=row["townhall"] or None)
        for tag, entry in sorted(players.items()):
            c.execute("INSERT INTO cwl_players(season_id,clan_tag,player_tag,account_id,name,townhall,extra_json,"
                      "source_run_id) VALUES (?,?,?,?,?,?,?,?)",
                      (season_id, group["clan"], tag, self.account(tag), entry["name"], entry["townhall"],
                       canonical_json({"legacy": {"basis": "final_rosters", "wars": entry["wars"]}}), run))
        for war in wars:
            war_id = self.create_war(war)
            c.execute("INSERT INTO cwl_rounds(season_id,round_no,war_tag,war_id,source_run_id) VALUES (?,?,?,?,?)",
                      (season_id, war["round"], war["tag"], war_id, run))
            # The tag has expired: the collector never polls it.
            c.execute("INSERT INTO war_collection_state(source_kind,scope_id,next_due_at,enabled,war_id,checkpoint_json) "
                      "VALUES ('cwl_war',?,?,0,?,?) ON CONFLICT(source_kind,scope_id) DO UPDATE SET enabled=0,"
                      "war_id=excluded.war_id", (war["tag"], at, war_id, canonical_json({"legacy": True})))
            self.stats[group["code"], group["season"]]["position_basis:" + war["basis"][0]] += 1

    def create_war(self, war):
        c, run = self.connection, self.run
        seen = min(war["seen"], self.at)
        values = {
            "type": "CWL", "type_basis": "war_tag", "war_tag": war["tag"], "preparation_at": war["prep"],
            "start_at": war["start"], "war_start_at": None, "end_at": war["end"], "state": "warEnded",
            "team_size": war["team_size"], "attacks_per_member": war["apm"], "battle_modifier_json": None,
            "first_seen_at": seen, "last_seen_at": seen, "final_at": None,
            # Complete for the family side only; no result, so no score is ever read from the partial opponent.
            "roster_complete": 1, "attacks_complete": 1, "result_complete": 0, "privacy_redacted": 0,
            "field_presence_json": canonical_json(WAR_FIELDS)}
        values["content_hash"] = sha256(canonical_json(values).encode("utf-8")).hexdigest()
        values["source_run_id"] = run
        war_id = c.execute(f"INSERT INTO wars({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                           tuple(values.values())).lastrowid
        war["war_id"] = war_id
        pair = sorted((war["clan"], war["opponent"]))
        own_basis, other_basis = war["basis"]
        derived = war["derived"]
        sides = {}
        for side, tag in enumerate(pair):
            if tag == war["clan"]:
                facts = (None, derived["stars"], derived["destruction"], derived["attacks"])
                extra = {"field_presence": [], "legacy": {"source": war["source"], "position_basis": own_basis},
                         "derived": ["attacks", "destruction", "stars"]}
            else:
                facts = (war["opponent_name"], None, None, None)
                extra = {"field_presence": ["name"] if war["opponent_name"] else [], "partial": True,
                         "legacy": {"source": war["source"], "position_basis": other_basis}}
            sides[tag] = side
            c.execute("INSERT INTO war_sides(war_id,side,clan_tag,name,stars,destruction,attacks,extra_json,source_run_id) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (war_id, side, tag, *facts, canonical_json(extra), run))
        key = ":".join(pair)
        for kind, value in (("cwl", war["tag"]), ("preparation", f"{key}:{war['prep']}"), ("end", f"{key}:{war['end']}")):
            c.execute("INSERT OR IGNORE INTO war_source_keys VALUES (?,?,?)", (kind, value, war_id))
        planned, ids = {}, {}
        own, other = sides[war["clan"]], sides[war["opponent"]]
        for tag, row in sorted(war["roster"].items()):
            extra = {"field_presence": MEMBER_FIELDS, "legacy": {
                "roster_state": row["roster_state"], "source": row["source"], "captured_at": row["captured_ts"] * SECOND,
                "position_basis": own_basis}}
            expected = row["attacks_expected"] or None
            ids[own, tag] = self.member(war_id, own, tag, row["player_name"] or None, row["townhall"] or None,
                                        row["map_position"] or None, expected, row["attacks_used"], extra)
            planned[own, tag] = (expected, row["attacks_used"])
        for tag, (name, townhall, position) in sorted(war["defenders"].items()):
            # Known only as a base that was hit: never a lineup, a zero or a miss.
            extra = {"field_presence": [field for field, value in zip(MEMBER_FIELDS, (position, name, tag, townhall))
                                        if value is not None], "partial": True,
                     "legacy": {"from_attacks": True, "position_basis": other_basis}}
            ids[other, tag] = self.member(war_id, other, tag, name, townhall, position, None, None, extra)
            planned[other, tag] = (None, None)
        attacks = {}
        for row in sorted(war["attacks"], key=lambda row: row["attack_order"]):
            extra = {"field_presence": ATTACK_FIELDS, "legacy": {
                "fresh_attack": bool(row["fresh_attack"]), "war_state": row["war_state"], "source": row["source"]}}
            c.execute("INSERT INTO war_attacks(war_id,attack_order,attacker_member_id,defender_member_id,stars,destruction,"
                      "duration,extra_json,source_run_id) VALUES (?,?,?,?,?,?,?,?,?)",
                      (war_id, row["attack_order"], ids[own, normalize_player_tag(row["player_tag"])],
                       ids[other, normalize_player_tag(row["defender_tag"])], row["stars"], row["destruction"],
                       row["duration"], canonical_json(extra), run))
            attacks[row["attack_order"]] = (row["stars"], row["destruction"])
        self.note_rows(war, "imported")
        counts = self.stats[war["code"], war["season"]]
        counts["opponent_members"] += len(war["defenders"])
        counts["unlinked_accounts"] += sum(self.account(tag) is None for tag in war["roster"])
        self.created[war_id] = {"values": values, "members": planned, "attacks": attacks}
        return war_id

    def member(self, war_id, side, tag, name, townhall, position, expected, used, extra):
        return self.connection.execute(
            "INSERT INTO war_members(war_id,side,player_tag,account_id,name,townhall,source_map_position,attacks_expected,"
            "attacks_used,opponent_attacks,extra_json,source_run_id) VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?)",
            (war_id, side, tag, self.account(tag), name, townhall, position, expected, used, canonical_json(extra),
             self.run)).lastrowid

    def alias(self, war):
        key = ("clan_health", war["old_id"], war["code"])
        try:
            map_legacy_war(self.connection, *key, war["war_id"], label="CWL", capture_state=war["state"],
                           captured_at=war["seen"], at=self.at)
        except ValueError as error:
            raise ImportMismatch(str(error), self.checks) from error
        self.aliases[key] = war["war_id"]

    # Checks --------------------------------------------------------------------------------

    def log_check(self, group):
        """The war log's league-week totals against the copied attacks: attacks, team size, and stars up to win bonuses."""
        wars, last = group["wars"], group["wars"][-1]["end"]
        attacks = sum(len(war["attacks"]) for war in wars)
        stars = sum(war["derived"]["stars"] for war in wars)
        sizes = sorted({war["team_size"] for war in wars})
        destruction = round(sum(war["derived"]["destruction"] for war in wars), 1)
        result = {"attacks": [attacks, None], "team_size": [sizes, None], "stars": [stars, None], "win_bonus": None,
                  "destruction": [destruction, None]}
        rows = self.connection.execute("SELECT * FROM cwl_log_summaries WHERE clan_tag=? AND end_at BETWEEN ? AND ?",
                                       (group["clan"], last - LOG_WINDOW, last + LOG_WINDOW)).fetchall()
        if len(rows) != 1:
            return {**result, "status": "ambiguous_summary" if rows else "no_summary"}
        log = rows[0]
        bonus = log["stars"] - stars if log["stars"] is not None else None
        result.update(attacks=[attacks, log["attacks"]], team_size=[sizes, log["team_size"]], stars=[stars, log["stars"]],
                      win_bonus=bonus, destruction=[destruction, log["destruction"]])
        if (log["attacks"] is not None and log["attacks"] != attacks or sizes != [log["team_size"]]
                or bonus is not None and (bonus % WIN_BONUS or not 0 <= bonus <= WIN_BONUS * len(wars))):
            return {**result, "status": "mismatch"}
        return {**result, "status": "matched" if None not in (log["attacks"], bonus) else "incomplete_summary"}

    def verify(self):
        c = self.connection
        for table in TABLES:
            counted = sum(n for (name, *_), n in self.ledger.items() if name == table)
            if counted != self.plan.source_rows[table]:
                raise ImportMismatch(f"source_rows_unaccounted:{table}", self.checks)
        for (source, legacy_id, code), war_id in self.aliases.items():
            row = c.execute("SELECT war_id FROM war_legacy_ids WHERE source=? AND legacy_id=? AND clan_code=?",
                            (source, legacy_id, code)).fetchone()
            if row is None or row[0] != war_id:
                raise ImportMismatch("alias_missing", self.checks)
        # Each war made here reads back exactly as planned and holds nothing else.
        for war_id, planned in self.created.items():
            stored = {(r[0], r[1]): (r[2], r[3]) for r in c.execute(
                "SELECT side,player_tag,attacks_expected,attacks_used FROM war_members WHERE war_id=?", (war_id,))}
            if stored != planned["members"]:
                raise ImportMismatch("lineup_mismatch", self.checks)
            stored = {r[0]: (r[1], r[2]) for r in c.execute(
                "SELECT attack_order,stars,destruction FROM war_attacks WHERE war_id=?", (war_id,))}
            if stored != planned["attacks"]:
                raise ImportMismatch("attack_mismatch", self.checks)
            row = c.execute("SELECT war_tag,preparation_at,end_at,team_size,attacks_per_member,state FROM wars WHERE id=?",
                            (war_id,)).fetchone()
            if tuple(row) != tuple(planned["values"][key] for key in (
                    "war_tag", "preparation_at", "end_at", "team_size", "attacks_per_member", "state")):
                raise ImportMismatch("war_mismatch", self.checks)
            state = c.execute("SELECT enabled FROM war_collection_state WHERE source_kind='cwl_war' AND scope_id=?",
                              (row["war_tag"],)).fetchone()
            if state is None or state[0] != 0:
                raise ImportMismatch("cwl_war_still_polled", self.checks)
        # A copied season is closed, so the collector never asks for its group again.
        for season_id, group in self.seasons.items():
            row = c.execute("SELECT state,final_at FROM cwl_seasons WHERE id=?", (season_id,)).fetchone()
            rounds = c.execute("SELECT round_no,war_id FROM cwl_rounds WHERE season_id=? ORDER BY round_no",
                               (season_id,)).fetchall()
            if (row["state"], row["final_at"] is not None) != ("ended", True) or [tuple(r) for r in rounds] != [
                    (war["round"], war["war_id"]) for war in group["wars"]]:
                raise ImportMismatch("cwl_season_mismatch", self.checks)
        for group in self.plan.groups:
            self.logs[group["code"], group["season"]] = self.log_check(group)
        if any(check["status"] == "mismatch" for check in self.logs.values()):
            self.checks = self.report()
            raise ImportMismatch("cwl_log_mismatch", self.checks)
        if c.execute("PRAGMA foreign_key_check").fetchone():
            raise ImportMismatch("family_foreign_key_check", self.checks)

    def report(self):
        outcomes = defaultdict(Counter)
        reasons = defaultdict(lambda: defaultdict(Counter))
        for (table, code, season, outcome), n in self.ledger.items():
            outcomes[table][outcome] += n
            if outcome.startswith("excluded:"):
                reasons[code, season][table][outcome[9:]] += n
        seasons = []
        for group in self.plan.groups:
            key = (group["code"], group["season"])
            wars = group["wars"]
            seasons.append({
                "clan": group["code"], "season_key": group["season"], "season_label": group["label"],
                "league": group["league"], "sources": group["sources"], "kept_record": key in self.kept,
                "wars": len(wars), "rounds": len(wars), "roster_rows": sum(len(war["roster"]) for war in wars),
                "attacks": sum(len(war["attacks"]) for war in wars), "team_size": sorted({war["team_size"] for war in wars}),
                **{name: self.stats[key][name] for name in ("opponent_members", "unlinked_accounts")},
                "position_basis": {name[15:]: n for name, n in sorted(self.stats[key].items())
                                   if name.startswith("position_basis:")},
                "excluded": {table: dict(sorted(counts.items())) for table, counts in sorted(reasons[key].items())},
                "log_check": self.logs.get(key)})
        conflicts = [item for group in self.plan.groups for war in group["wars"] for item in war["conflicts"]]
        made = list(self.created.values())
        # Rows whose war the old store never saved, by clan.
        unplaced = {code: {table: dict(sorted(counts.items())) for table, counts in sorted(tables.items())}
                    for (code, season), tables in sorted(reasons.items()) if season == ""}
        return {"cutoff": CUTOFF, "source_rows": {table: self.plan.source_rows[table] for table in TABLES},
                "outcomes": {table: dict(sorted(counts.items())) for table, counts in sorted(outcomes.items())},
                "seasons": seasons, "left_out": self.plan.left_out, "unplaced": unplaced,
                "log_checks": dict(sorted(Counter(check["status"] for check in self.logs.values()).items())),
                "inserted": {"seasons": len(self.seasons), "wars": len(made),
                             "lineup_rows": sum(e != (None, None) for war in made for e in war["members"].values()),
                             "opponent_members": sum(e == (None, None) for war in made for e in war["members"].values()),
                             "attacks": sum(len(war["attacks"]) for war in made)},
                "activity_conflicts": conflicts[:MAX_LISTED]}


def write_cwl(connection, frozen, clans):
    """The CWL part of the copy, inside the caller's transaction; returns its result, marker written."""
    ledger = Counter()
    plan = CwlPlan(frozen, clans, ledger)
    marker = connection.execute("SELECT * FROM imports WHERE topic=?", (TOPIC,)).fetchone()
    if marker is not None and marker["source_digest"] == frozen.digest:
        return {"imported": False, "source_version": frozen.version, "source_digest": frozen.digest,
                "frozen_at": marker["frozen_at"], "checks": json.loads(marker["checks_json"])}
    item = receipt(connection, COLLECTOR, TOPIC, frozen.frozen_at, sum(len(group["wars"]) for group in plan.groups))
    run = connection.execute("SELECT run_id FROM collection_items WHERE id=?", (item,)).fetchone()[0]
    importer = CwlImporter(connection, frozen, plan, ledger, run)
    importer.write()
    importer.verify()
    checks = importer.checks = importer.report()
    written = checks["inserted"]["lineup_rows"] + checks["inserted"]["opponent_members"] + checks["inserted"]["attacks"]
    connection.execute("UPDATE collection_runs SET rows_written=? WHERE id=?", (written, run))
    connection.execute("UPDATE collection_items SET written_count=? WHERE id=?", (written, item))
    mapping = digest(sorted([*key, war_id] for key, war_id in importer.aliases.items()))
    connection.execute(
        "INSERT INTO imports VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(topic) DO UPDATE SET "
        "source_version=excluded.source_version,source_digest=excluded.source_digest,frozen_at=excluded.frozen_at,"
        "current_links_digest=excluded.current_links_digest,checked_at=excluded.checked_at,"
        "checks_json=excluded.checks_json,source_item_id=excluded.source_item_id",
        (TOPIC, frozen.version, frozen.digest, frozen.frozen_at, mapping, frozen.frozen_at, canonical_json(checks), item))
    return {"imported": True, "source_version": frozen.version, "source_digest": frozen.digest,
            "frozen_at": frozen.frozen_at, "checks": checks}


def import_cwl_wars(connection, health_path=None, frozen=None, *, clans=None):
    """Caller holds the family writer. A repeat with unchanged sources adds nothing; changed sources go through
    the same checked copy, which keeps every week the record already holds."""
    frozen = snapshot_wars(health_path) if frozen is None else frozen
    clans = {code: clan.tag for code, clan in CLANS.items()} if clans is None else clans
    with sqlite_transaction(connection, immediate=True):
        return write_cwl(connection, frozen, clans)
