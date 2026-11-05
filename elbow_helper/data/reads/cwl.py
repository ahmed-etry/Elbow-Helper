"""Stored league weeks, native timestamps, lineups, attacks and source coverage."""

from collections import defaultdict
import json

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.persistence.revisions import sqlite_revision

from ..store.api_fields import unpack
from .core import MAX_SELECTORS, RecordReads, placeholders, tags, timestamp
from .coverage import envelope, war_side_coverage, war_signal
from .wars import clan_tags, member_rows, war_view

LEAGUE_PATH = "/warLeague/name"
STATES = ("preparation", "inWar", "warEnded")
MAX_KEYS = MAX_SELECTORS


def old_id(war_tag):
    return f"CWL:{war_tag}"


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
    """Bounded league facts; all timestamps are UTC epoch microseconds."""

    def __init__(self, store, clans=None):
        super().__init__(store)
        # A finished week's league never changes; it is looked up once per process.
        self.leagues = {}

    def cache_revision(self):
        """The record's commit revision; any write, a league check included, changes it."""
        path = getattr(self.store, "path", None)
        return sqlite_revision(path) if path is not None else None

    # League weeks ------------------------------------------------------------------------------

    async def weeks(self, clans, *, seasons=None, war_tags=None, ended=True, details=True, as_of=None):
        """The clans' league weeks, oldest first, each with its wars in round order.

        ``seasons`` keeps weeks with these stored season keys; ``war_tags`` keeps
        these wars. With ``ended``, only ended wars are listed, and a week
        without one is left out. ``details`` adds stored members, attacks and participation.
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
            wars = [dict(row) for row in kept]
            for war in wars:
                war["coverage"] = war_side_coverage(connection, war["record_id"], clan)
                war["source_coverage"] = envelope([war_signal(connection, "cwl_war", war["war_tag"], at)], at=at)
            if details:
                self._details(connection, wars, clan)
            result.append({"season_id": season_id, "season_key": key, "label": label, "league_name": league,
                           "clan_tag": clan, "final": first["season_final_at"] is not None,
                           "legacy": bool(legacy), "rounds": len({row["round_no"] for row in rows}), "wars": wars})
        result.sort(key=lambda week: (max(war["end_at"] for war in week["wars"]), week["clan_tag"], week["season_key"]))
        signals = [war_signal(connection, "cwl_group", clan, at) for clan in clans]
        missing = []
        for ref in connection.execute(
                "SELECT s.season_key,src.clan_tag,r.round_no,r.war_tag,w.id AS war_id,w.first_seen_at,"
                "o.war_id AS own_war_id "
                "FROM cwl_season_sources src JOIN cwl_seasons s ON s.id=src.season_id "
                "JOIN cwl_rounds r ON r.season_id=s.id LEFT JOIN wars w ON w.war_tag=r.war_tag "
                "LEFT JOIN war_sides o ON o.war_id=w.id AND o.clan_tag=src.clan_tag "
                f"WHERE src.clan_tag IN ({placeholders(clans)})", clans):
            if seasons is not None and ref["season_key"] not in seasons or selected is not None and ref["war_tag"] not in selected:
                continue
            if ref["war_id"] is not None and ref["first_seen_at"] <= at and ref["own_war_id"] is None:
                continue
            signals.append(war_signal(connection, "cwl_war", ref["war_tag"], at))
            if ref["war_id"] is None or ref["first_seen_at"] > at:
                missing.append({"clan_tag": ref["clan_tag"], "round": ref["round_no"], "war_tag": ref["war_tag"]})
        coverage = envelope(signals, at=at)
        coverage["missing_wars"] = missing
        coverage["incomplete_wars"] = [
            {"war_id": war["record_id"], "clan_tag": week["clan_tag"], "coverage": war["coverage"]}
            for week in result for war in week["wars"]
            if not all(war["coverage"][side][field] for side in ("own", "opponent")
                       for field in ("lineup_complete", "attacks_complete"))]
        coverage["complete"] &= not (missing or coverage["incomplete_wars"])
        return {"weeks": result, "coverage": coverage}

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
    def _details(connection, wars, clan):
        """Stored member and attack facts, without feature columns or position changes."""
        ids = json.dumps([war["record_id"] for war in wars])
        members, attacks = defaultdict(list), defaultdict(list)
        for member in connection.execute(
                "SELECT * FROM war_members WHERE war_id IN (SELECT value FROM json_each(?))", (ids,)):
            members[member["war_id"]].append(dict(member))
        for attack in connection.execute(
                "SELECT * FROM war_attacks WHERE war_id IN (SELECT value FROM json_each(?)) "
                "AND COALESCE(json_extract(extra_json,'$.stub'),0)=0 ORDER BY war_id,attack_order", (ids,)):
            attacks[attack["war_id"]].append(dict(attack))
        for war in wars:
            war["members"] = members[war["record_id"]]
            war["attacks"] = attacks[war["record_id"]]
            war["participation"] = member_rows(
                connection, {"id": war["record_id"], "type": "CWL", "state": war["state"], "end_at": war["end_at"]},
                clan, war["coverage"])

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
                found[row["id"]] = {"war_id": row["id"], "war_tag": row["war_tag"], "type": row["type"], "state": row["state"],
                                    "clan_tags": [side[0] for side in connection.execute(
                                        "SELECT clan_tag FROM war_sides WHERE war_id=? ORDER BY side", (row["id"],))]}
            result[key] = [found[war_id] for war_id in sorted(found)]
        signals = []
        for matches in result.values():
            for war in matches:
                if war["war_tag"]:
                    signals.append(war_signal(connection, "cwl_war", war["war_tag"], at))
                for clan in war["clan_tags"]:
                    signals.append(war_signal(connection, "cwl_group", clan, at))
        coverage = envelope(signals, at=at)
        coverage["observed_war_ids"] = sorted({war["war_id"] for matches in result.values() for war in matches})
        coverage["unresolved_keys"] = [key for key in keys if not result[key]]
        coverage["complete"] &= not coverage["unresolved_keys"]
        return {"matches": result, "coverage": coverage}

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
                  "round_count": 0, "wars": [], "signal": signal, "coverage": envelope([signal], at=at)}
        if season is None:
            return result
        metadata = json.loads(season["group_metadata_json"])
        result.update(season_id=season["id"], season_key=season["season_key"], league=season["league_name"],
                      round_count=metadata.get("round_count") or 0)
        missing, signals = [], [signal]
        for ref in connection.execute("SELECT round_no,war_tag FROM cwl_rounds WHERE season_id=? ORDER BY round_no,war_tag",
                                      (season["id"],)):
            source = war_signal(connection, "cwl_war", ref["war_tag"], at)
            row = connection.execute("SELECT * FROM wars WHERE war_tag=? AND first_seen_at<=?", (ref["war_tag"], at)).fetchone()
            if row is None or row["state"] not in STATES:
                signals.append(source)
                missing.append(ref["war_tag"])
                continue
            if not connection.execute("SELECT 1 FROM war_sides WHERE war_id=? AND clan_tag=?", (row["id"], clan)).fetchone():
                continue
            signals.append(source)
            result["wars"].append({"round": ref["round_no"], "war_tag": ref["war_tag"],
                                   "war": war_view(connection, row, clan, at, lineups=True),
                                   "signal": source})
        result["coverage"] = envelope(signals, at=at)
        result["coverage"]["missing_wars"] = missing
        result["coverage"]["complete"] &= not missing
        return result

    # Names --------------------------------------------------------------------------------------

    async def account_names(self, clans, as_of=None):
        """Every name an account carried in a family clan's lineup, regular and league wars alike."""
        return await self._read(self._account_names, clan_tags(clans), timestamp(as_of))

    @staticmethod
    def _account_names(connection, clans, at):
        rows = [{"player_tag": row["player_tag"], "player_name": row["name"]} for row in connection.execute(
            "SELECT DISTINCT m.player_tag,m.name FROM war_members m JOIN war_sides s ON s.war_id=m.war_id "
            f"AND s.side=m.side JOIN wars w ON w.id=m.war_id WHERE s.clan_tag IN ({placeholders(clans)}) "
            "AND m.name IS NOT NULL AND w.first_seen_at<=? ORDER BY m.player_tag,m.name", (*clans, at))]

        signals = [war_signal(connection, kind, clan, at) for clan in clans
                   for kind in ("current_war", "war_log", "cwl_group")]
        coverage = envelope(signals, at=at)
        coverage["name_history_complete"] = False
        return {"rows": rows, "coverage": coverage}
