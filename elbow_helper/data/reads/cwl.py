"""League weeks and league groups from the family record, in the shapes the league features use.

A league week is one family clan's part of one league group: its rounds' wars,
each with the clan's final lineup and its attacks. Weeks copied from old clan
health keep their old season key, label, league and positions. Weeks the
collector saved take the month the collector stored for their group.
Clash's league group names no league, so a collector week takes the clan's war
league as its stored profile showed it when the week's last war ended.

War IDs keep the old ``CWL:<war tag>`` form, so selections made from earlier
answers keep working. Lineup positions run 1..team size as the old store
numbered them. A count the record does not know stays None.
"""

from collections import defaultdict
import json

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.persistence.revisions import sqlite_revision

from ..store.api_fields import unpack
from .core import RecordReads, placeholders, tags, timestamp
from .coverage import envelope, war_side_coverage, war_signal
from .wars import clan_tags, member_rows, war_view

SECOND = 1_000_000
LEAGUE_PATH = "/warLeague/name"
SOURCE = "family_record"
STATES = ("preparation", "inWar", "warEnded")
# At most this many war IDs are resolved in one read, as the old scoring read allowed.
MAX_KEYS = 56


def old_id(war_tag):
    return f"CWL:{war_tag}"


def seconds(value):
    return None if value is None else value // SECOND


def renumbered(members, keep):
    """Positions 1..n by stored map position, as the old store numbered a lineup.

    A side copied from old clan health, or one with an unknown position, keeps
    its stored positions.
    """
    if keep or any(member["source_map_position"] is None for member in members):
        return {member["id"]: member["source_map_position"] for member in members}
    ordered = sorted(members, key=lambda member: (member["source_map_position"], member["player_tag"]))
    return {member["id"]: index for index, member in enumerate(ordered, 1)}


def profile_league(connection, clan, at):
    """The clan's war league as its stored profile last showed it at or before ``at``; None when unknown."""
    record = connection.execute("SELECT id,state_z FROM api_records WHERE source_kind='clan_profile' AND scope_id=?",
                                (clan,)).fetchone()
    if record is None:
        return None
    paths, _ = unpack(record["state_z"])
    if LEAGUE_PATH not in paths:
        return None
    key = str(paths.index(LEAGUE_PATH))
    for row in connection.execute("SELECT patch_z FROM api_field_changes WHERE record_id=? AND observed_at<=? "
                                  "ORDER BY source_run_id DESC", (record["id"], at)):
        changed, removed = unpack(row[0])
        if key in changed:
            return changed[key] if isinstance(changed[key], str) and changed[key] else None
        if key in removed:
            return None
    return None


class CwlReads(RecordReads):
    """Bounded league reads; times are UTC epoch microseconds, old-shape times UTC seconds."""

    def __init__(self, store, clans=None):
        super().__init__(store)
        clans = CLANS if clans is None else clans
        self.codes = {clan.tag: code for code, clan in clans.items()}
        # A finished week's league never changes; it is looked up once per process.
        self.leagues = {}

    def cache_revision(self):
        """The record's commit revision; any write, a league check included, changes it."""
        path = getattr(self.store, "path", None)
        return sqlite_revision(path) if path is not None else None

    # League weeks ------------------------------------------------------------------------------

    async def weeks(self, clans, *, seasons=None, war_tags=None, ended=True, details=True, as_of=None):
        """The clans' league weeks, oldest first, each with its wars in round order.

        ``seasons`` keeps weeks with these old season keys; ``war_tags`` keeps
        these wars. With ``ended``, only ended wars are listed, and a week
        without one is left out. ``details`` adds each war's roster and attacks.
        """
        selected = None if war_tags is None else set(tags(war_tags))
        return await self._read(self._weeks, clan_tags(clans), None if seasons is None else set(seasons),
                                selected, bool(ended), bool(details), timestamp(as_of))

    def _weeks(self, connection, clans, seasons, selected, ended, details, at):
        weeks = {}
        for row in connection.execute(
                "SELECT s.id AS season_id,s.season_key,s.league_name,s.final_at AS season_final_at,"
                "s.group_metadata_json,src.clan_tag AS family_tag,r.round_no,w.id AS record_id,w.war_tag,w.state,"
                "w.team_size,w.attacks_per_member,w.preparation_at,w.start_at,w.end_at,w.last_seen_at,"
                "o.side AS own_side,o.extra_json AS own_extra,x.clan_tag AS opponent_tag,x.name AS opponent_name,"
                "x.extra_json AS opponent_extra FROM cwl_season_sources src JOIN cwl_seasons s ON s.id=src.season_id "
                "JOIN cwl_rounds r ON r.season_id=s.id JOIN wars w ON w.war_tag=r.war_tag "
                "JOIN war_sides o ON o.war_id=w.id AND o.clan_tag=src.clan_tag "
                "JOIN war_sides x ON x.war_id=w.id AND x.side!=o.side "
                f"WHERE src.clan_tag IN ({placeholders(clans)}) AND w.type='CWL' AND w.first_seen_at<=? "
                "ORDER BY s.id,src.clan_tag,r.round_no,w.end_at,w.war_tag", (*clans, at)):
            weeks.setdefault((row["season_id"], row["family_tag"]), []).append(row)
        result = []
        for (season_id, clan), rows in weeks.items():
            first = rows[0]
            metadata = json.loads(first["group_metadata_json"])
            legacy = metadata.get("legacy") if isinstance(metadata.get("legacy"), dict) else None
            # The key is read as stored: the collector's month, or a copied week's old key.
            key = first["season_key"]
            if legacy:
                label, league = legacy.get("season_label") or key, first["league_name"]
            else:
                label = key
                league = first["league_name"] or self._league(connection, season_id, clan, rows, at)
            if seasons is not None and key not in seasons:
                continue
            kept = [row for row in rows if (not ended or row["state"] == "warEnded")
                    and (selected is None or row["war_tag"] in selected)]
            if not kept:
                continue
            code = self.codes.get(clan, "")
            wars = [self._war(row, code, clan, key, label, league) for row in kept]
            for row, war in zip(kept, wars):
                war["coverage"] = war_side_coverage(connection, row["record_id"], clan)
            if details:
                self._details(connection, kept, wars)
            result.append({"season_id": season_id, "season": key, "label": label, "league": league or "",
                           "clan_tag": clan, "clan_code": code, "final": first["season_final_at"] is not None,
                           "legacy": bool(legacy), "rounds": len({row["round_no"] for row in rows}), "wars": wars})
        result.sort(key=lambda week: (max(war["end_ts"] for war in week["wars"]), week["clan_code"], week["season"]))
        signals = [war_signal(connection, "cwl_group", clan, at) for clan in clans]
        return {"weeks": result, "coverage": envelope(signals, at=at)}

    def _league(self, connection, season_id, clan, rows, at):
        # Before the week's last war ends the league is the one being played.
        ended = all(row["state"] == "warEnded" for row in rows)
        moment = min(at, max(row["end_at"] for row in rows)) if ended else at
        cached = self.leagues.get((season_id, clan))
        if cached is not None:
            return cached
        league = profile_league(connection, clan, moment)
        if ended and league:
            self.leagues[season_id, clan] = league
        return league

    @staticmethod
    def _war(row, code, clan, key, label, league):
        extra = json.loads(row["own_extra"])
        legacy = extra.get("legacy") if isinstance(extra.get("legacy"), dict) else {}
        return {"war_id": old_id(row["war_tag"]), "war_type": "CWL", "clan_code": code, "clan_tag": clan,
                "opponent_tag": row["opponent_tag"], "opponent_name": row["opponent_name"] or "",
                "cwl_season": key, "cwl_season_label": label, "cwl_league": league or "", "cwl_round": row["round_no"],
                "team_size": row["team_size"], "attacks_per_member": row["attacks_per_member"], "state": row["state"],
                "preparation_start_ts": seconds(row["preparation_at"]) or 0, "start_ts": seconds(row["start_at"]) or 0,
                "end_ts": seconds(row["end_at"]), "last_seen_ts": seconds(row["last_seen_at"]),
                "source": legacy.get("source") or SOURCE, "record_war_id": row["record_id"]}

    @staticmethod
    def _details(connection, rows, wars):
        """Each war's own lineup as roster rows and the clan's attacks as attack rows, in the old shapes."""
        ids = json.dumps([row["record_id"] for row in rows])
        members, attacks = defaultdict(list), defaultdict(list)
        for member in connection.execute(
                "SELECT id,war_id,side,player_tag,name,townhall,source_map_position,attacks_expected,attacks_used,"
                "extra_json FROM war_members WHERE war_id IN (SELECT value FROM json_each(?))", (ids,)):
            members[member["war_id"]].append(member)
        for attack in connection.execute(
                "SELECT war_id,attack_order,attacker_member_id,defender_member_id,stars,destruction,duration,extra_json "
                "FROM war_attacks WHERE war_id IN (SELECT value FROM json_each(?)) "
                "AND COALESCE(json_extract(extra_json,'$.stub'),0)=0 ORDER BY war_id,attack_order", (ids,)):
            attacks[attack["war_id"]].append(attack)
        for row, war in zip(rows, wars):
            participation = {member["player_tag"]: member for member in member_rows(
                connection, {"id": row["record_id"], "type": "CWL", "state": row["state"], "end_at": row["end_at"]},
                war["clan_tag"], war["coverage"])}
            own_extra, other_extra = json.loads(row["own_extra"]), json.loads(row["opponent_extra"])
            own = [m for m in members[row["record_id"]] if m["side"] == row["own_side"]]
            other = [m for m in members[row["record_id"]] if m["side"] != row["own_side"]]
            places = renumbered(own, "legacy" in own_extra)
            places.update(renumbered(other, "legacy" in other_extra or other_extra.get("partial")))
            by_id = {m["id"]: m for m in own + other}
            own_tags = {m["player_tag"]: m for m in own}
            captured = {}
            for member in own:
                legacy = json.loads(member["extra_json"]).get("legacy")
                captured[member["id"]] = (seconds(legacy["captured_at"]) if isinstance(legacy, dict)
                                          and legacy.get("captured_at") else war["last_seen_ts"])
            war["roster"] = [{
                "war_id": war["war_id"], "clan_code": war["clan_code"], "player_tag": m["player_tag"],
                "player_name": m["name"] or m["player_tag"], "townhall": m["townhall"] or 0,
                "map_position": places[m["id"]] or 0, "attacks_expected": m["attacks_expected"],
                "attacks_used": m["attacks_used"], "roster_state": war["state"], "captured_ts": captured[m["id"]],
                "missed": participation[m["player_tag"]]["missed"],
                "source": war["source"]} for m in sorted(own, key=lambda m: (places[m["id"]] or 0, m["name"] or ""))]
            first = {}
            for attack in attacks[row["record_id"]]:
                extra = json.loads(attack["extra_json"])
                defender = by_id[attack["defender_member_id"]]["player_tag"] if attack["defender_member_id"] in by_id \
                    else extra.get("defender_tag")
                first.setdefault(defender, attack["attack_order"])
            war["attacks"] = []
            for attack in attacks[row["record_id"]]:
                extra = json.loads(attack["extra_json"])
                attacker = by_id.get(attack["attacker_member_id"])
                tag = attacker["player_tag"] if attacker else extra.get("attacker_tag")
                if tag not in own_tags or attacker is not None and attacker["side"] != row["own_side"]:
                    continue
                attacker = own_tags[tag]
                defender = by_id.get(attack["defender_member_id"])
                defender_tag = defender["player_tag"] if defender else extra.get("defender_tag")
                legacy = extra.get("legacy") if isinstance(extra.get("legacy"), dict) else {}
                fresh = legacy["fresh_attack"] if "fresh_attack" in legacy else first.get(defender_tag) == attack["attack_order"]
                war["attacks"].append({
                    "war_id": war["war_id"], "war_type": "CWL", "clan_code": war["clan_code"],
                    "clan_tag": war["clan_tag"], "end_ts": war["end_ts"], "war_state": war["state"],
                    "player_tag": tag, "player_name": attacker["name"] or tag, "attack_order": attack["attack_order"],
                    "defender_tag": defender_tag or "", "defender_name": (defender["name"] if defender else None)
                    or defender_tag or "Unknown",
                    "defender_map_position": (places.get(defender["id"]) if defender else None) or 0,
                    "defender_townhall": (defender["townhall"] if defender else None) or 0,
                    "stars": attack["stars"], "destruction": attack["destruction"], "fresh_attack": int(bool(fresh)),
                    "duration": attack["duration"] or 0, "source": war["source"]})
            war["attacks"].sort(key=lambda attack: (attack["attack_order"], attack["player_name"]))

    # War IDs ------------------------------------------------------------------------------------

    async def resolve(self, keys, as_of=None):
        """The record wars each chosen ID names: a war tag, or an old ``CWL:`` or other clan-health war ID.

        Each key maps to its wars as {war_tag, type, state, clan_tags}; an unknown key maps to none.
        """
        keys = tuple(keys)
        if not 1 <= len(keys) <= MAX_KEYS or any(not isinstance(key, str) or not key for key in keys):
            raise ValueError(f"Expected 1..{MAX_KEYS} war IDs")
        return await self._read(self._resolve, keys, timestamp(as_of))

    @staticmethod
    def _resolve(connection, keys, at):
        result = {}
        for key in keys:
            found = {}
            tag = normalize_player_tag(key)
            rows = connection.execute("SELECT * FROM wars WHERE war_tag=? AND first_seen_at<=?", (tag, at)).fetchall() \
                if tag else []
            rows += connection.execute(
                "SELECT w.* FROM war_legacy_ids l JOIN wars w ON w.id=l.war_id WHERE l.source='clan_health' "
                "AND l.legacy_id IN (?,?) AND w.first_seen_at<=?", (key, old_id(key), at)).fetchall()
            for row in rows:
                found[row["id"]] = {"war_tag": row["war_tag"], "type": row["type"], "state": row["state"],
                                    "clan_tags": [side[0] for side in connection.execute(
                                        "SELECT clan_tag FROM war_sides WHERE war_id=? ORDER BY side", (row["id"],))]}
            result[key] = [found[war_id] for war_id in sorted(found)]
        return result

    # League group as it stands -----------------------------------------------------------------

    async def group(self, clan, as_of=None):
        """The clan's league group as its latest check saw it, with the clan's wars in round order.

        Each war carries its record view with both lineups and its own source's
        status. No group when the latest good check found none.
        """
        return await self._read(self._group, clan_tags([clan])[0], timestamp(as_of))

    @staticmethod
    def _group(connection, clan, at):
        signal = war_signal(connection, "cwl_group", clan, at)
        observed = signal["observed"]
        season = None
        if observed.get("state") not in (None, "notInWar") and observed.get("season_id"):
            season = connection.execute("SELECT * FROM cwl_seasons WHERE id=?", (observed["season_id"],)).fetchone()
        result = {"clan_tag": clan, "state": observed.get("state"), "season_id": None, "season_key": None,
                  "round_count": 0, "wars": [], "signal": signal}
        if season is None:
            return result
        metadata = json.loads(season["group_metadata_json"])
        result.update(season_id=season["id"], season_key=season["season_key"], league=season["league_name"],
                      round_count=metadata.get("round_count") or 0)
        for ref in connection.execute("SELECT round_no,war_tag FROM cwl_rounds WHERE season_id=? ORDER BY round_no,war_tag",
                                      (season["id"],)):
            row = connection.execute("SELECT * FROM wars WHERE war_tag=? AND first_seen_at<=?", (ref["war_tag"], at)).fetchone()
            if row is None or row["state"] not in STATES or not connection.execute(
                    "SELECT 1 FROM war_sides WHERE war_id=? AND clan_tag=?", (row["id"], clan)).fetchone():
                continue
            result["wars"].append({"round": ref["round_no"], "war_tag": ref["war_tag"],
                                   "war": war_view(connection, row, clan, at, lineups=True),
                                   "signal": war_signal(connection, "cwl_war", ref["war_tag"], at)})
        return result

    # Names --------------------------------------------------------------------------------------

    async def account_names(self, clans, as_of=None):
        """Every name an account carried in a family clan's lineup, regular and league wars alike."""
        return await self._read(self._account_names, clan_tags(clans), timestamp(as_of))

    @staticmethod
    def _account_names(connection, clans, at):
        return [{"player_tag": row["player_tag"], "player_name": row["name"]} for row in connection.execute(
            "SELECT DISTINCT m.player_tag,m.name FROM war_members m JOIN war_sides s ON s.war_id=m.war_id "
            f"AND s.side=m.side JOIN wars w ON w.id=m.war_id WHERE s.clan_tag IN ({placeholders(clans)}) "
            "AND m.name IS NOT NULL AND w.first_seen_at<=? ORDER BY m.player_tag,m.name", (*clans, at))]
