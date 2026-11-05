"""Clan-facing war reads: boards, completed wars, participation, attacks and log counts.

Every war is shown facing the requested clan. Regular wars are the default;
friendly and league wars are read only when asked for, and each row keeps
its type and what decided it. A count the record cannot prove stays None:
an incomplete lineup or attack list never becomes a zero.
"""

import json

from elbow_helper.infrastructure.persistence.revisions import sqlite_revision

from .core import RecordReads, page, pagination, placeholders, tags, timestamp
from .coverage import envelope, war_side_coverage, war_signal
from .people import ownership

TYPES = ("CWL", "FRIENDLY", "REG")
REGULAR = ("REG",)
TIME_FIELDS = ("end_at", "observed_at")
STATES = ("preparation", "inWar", "warEnded")
# A window may select at most this many clan-facing wars.
MAX_WARS = 1000


def war_types(values, allowed=TYPES):
    values = tuple(sorted(set(values)))
    if not values or any(value not in allowed for value in values):
        raise ValueError(f"Expected war types from {', '.join(allowed)}")
    return values


def clan_tags(values):
    selected = tags(values)
    if not selected:
        raise ValueError("Expected at least one clan tag")
    return selected


def window(after, before):
    if after is None or before is None:
        raise ValueError("Expected explicit window bounds")
    after, before = timestamp(after), timestamp(before)
    if before <= after:
        raise ValueError("Expected a window that ends after it starts")
    return after, before


def score_result(own, other):
    scores = (own["stars"], own["destruction"]), (other["stars"], other["destruction"])
    if None in scores[0] or None in scores[1]:
        return None
    return "win" if scores[0] > scores[1] else "lose" if scores[0] < scores[1] else "tie"


def side_view(connection, war, row):
    total = (war["team_size"] * war["attacks_per_member"]
             if war["team_size"] is not None and war["attacks_per_member"] is not None else None)
    counts = [{"townhall": level, "count": count} for level, count in connection.execute(
        "SELECT townhall,COUNT(*) FROM war_members WHERE war_id=? AND side=? AND townhall IS NOT NULL "
        "GROUP BY townhall ORDER BY townhall DESC", (war["id"], row["side"]))]
    return {"clan_tag": row["clan_tag"], "name": row["name"], "level": row["level"],
            "badge": json.loads(row["badge_json"]) if row["badge_json"] else None,
            "stars": row["stars"], "destruction": row["destruction"], "attacks_used": row["attacks"],
            "attacks_left": max(0, total - row["attacks"]) if total is not None and row["attacks"] is not None else None,
            "townhall_counts": counts}


def lineup(connection, war_id, side):
    """One side's lineup by map position, each account with its stored attacks in order.

    An attack whose attacker is unknown cannot be placed; coverage says so.
    """
    attacks = {}
    for row in connection.execute(
            "SELECT a.attacker_member_id,a.attack_order,a.stars,a.destruction,a.duration,"
            "COALESCE(d.player_tag,json_extract(a.extra_json,'$.defender_tag')) AS defender_tag "
            "FROM war_attacks a LEFT JOIN war_members d ON d.id=a.defender_member_id WHERE a.war_id=? "
            "AND a.attacker_member_id IS NOT NULL AND COALESCE(json_extract(a.extra_json,'$.stub'),0)=0 "
            "ORDER BY a.attack_order", (war_id,)):
        attacks.setdefault(row["attacker_member_id"], []).append(
            {key: row[key] for key in ("attack_order", "defender_tag", "stars", "destruction", "duration")})
    return [{"player_tag": row["player_tag"], "name": row["name"], "townhall": row["townhall"],
             "map_position": row["source_map_position"], "attacks_expected": row["attacks_expected"],
             "attacks_used": row["attacks_used"], "attacks": attacks.get(row["id"], [])}
            for row in connection.execute(
                "SELECT id,player_tag,name,townhall,source_map_position,attacks_expected,attacks_used FROM war_members "
                "WHERE war_id=? AND side=? ORDER BY source_map_position IS NULL,source_map_position,player_tag",
                (war_id, side))]


def war_view(connection, war, clan, at, lineups=False):
    sides = {row["clan_tag"]: row for row in connection.execute("SELECT * FROM war_sides WHERE war_id=?", (war["id"],))}
    own, other = sides[clan], next(row for tag, row in sides.items() if tag != clan)
    # Clash's log result wins; a final score decides only when the log has none.
    result, basis = own["result"], "war_log" if own["result"] else None
    if result is None and war["state"] == "warEnded" and war["result_complete"]:
        result = score_result(own, other)
        basis = "final_score" if result else None
    coverage = war_side_coverage(connection, war["id"], clan)
    # Facts are kept as last changed; a later change cannot be undone for an older as_of.
    coverage["changed_after_as_of"] = war["last_seen_at"] > at
    legacy = [dict(row) for row in connection.execute(
        "SELECT source,legacy_id,clan_code,legacy_label,capture_state,captured_at FROM war_legacy_ids "
        "WHERE war_id=? ORDER BY source,legacy_id,clan_code", (war["id"],))]
    view = {"war_id": war["id"], "clan_tag": clan, "type": war["type"], "type_basis": war["type_basis"],
            "war_tag": war["war_tag"], "state": war["state"], "preparation_at": war["preparation_at"],
            "start_at": war["start_at"], "end_at": war["end_at"], "team_size": war["team_size"],
            "attacks_per_member": war["attacks_per_member"],
            "battle_modifier": json.loads(war["battle_modifier_json"]) if war["battle_modifier_json"] else None,
            "first_seen_at": war["first_seen_at"], "last_seen_at": war["last_seen_at"], "final_at": war["final_at"],
            "result": result, "result_basis": basis, "clan": side_view(connection, war, own),
            "opponent": side_view(connection, war, other), "legacy_ids": legacy, "coverage": coverage}
    if lineups:
        for key, row in (("clan", own), ("opponent", other)):
            view[key]["members"] = lineup(connection, war["id"], row["side"])
    return view


def side_of(connection, war_id, clan):
    return connection.execute("SELECT side FROM war_sides WHERE war_id=? AND clan_tag=?", (war_id, clan)).fetchone()[0]


def member_rows(connection, war, clan, coverage):
    """One row per account in the clan's lineup; missed only once the war ended and the count is final.

    Counts are final when every attack is stored, or when the accounts' used
    attacks add up to the clan's reported total: no attack can have come after
    the capture. Early wars copied from old participation rows prove it this way.
    An account that used every attack is final either way. A total the copy
    summed from those same used attacks proves nothing, so it never counts.
    """
    lineup = connection.execute(
        "SELECT m.player_tag,m.name,m.townhall,m.source_map_position,m.attacks_expected,m.attacks_used,"
        "COUNT(a.attack_order) AS measured,COALESCE(SUM(a.stars),0) AS stars,COALESCE(SUM(a.stars=3),0) AS triples,"
        "COALESCE(SUM(a.destruction),0) AS destruction FROM war_members m LEFT JOIN war_attacks a "
        "ON a.war_id=m.war_id AND a.attacker_member_id=m.id AND COALESCE(json_extract(a.extra_json,'$.stub'),0)=0 "
        "WHERE m.war_id=? AND m.side=? GROUP BY m.id ORDER BY m.player_tag",
        (war["id"], side_of(connection, war["id"], clan))).fetchall()
    reported = coverage["own"]["attacks_reported"]
    extra = connection.execute("SELECT extra_json FROM war_sides WHERE war_id=? AND clan_tag=?",
                               (war["id"], clan)).fetchone()[0]
    counted = (reported is not None and "attacks" not in json.loads(extra).get("derived", ()) and bool(lineup)
               and all(row["attacks_expected"] is not None and row["attacks_used"] is not None for row in lineup)
               and sum(row["attacks_used"] for row in lineup) == reported)
    final = war["state"] == "warEnded" and (coverage["own"]["attacks_complete"] or counted)
    rows = []
    for row in lineup:
        expected, used = row["attacks_expected"], row["attacks_used"]
        counts = expected is not None and used is not None
        known = counts and war["state"] == "warEnded" and (final or used >= expected)
        rows.append({"war_id": war["id"], "clan_tag": clan, "type": war["type"], "end_at": war["end_at"],
                     "player_tag": row["player_tag"], "player_name": row["name"], "townhall": row["townhall"],
                     "map_position": row["source_map_position"], "attacks_expected": expected, "attacks_used": used,
                     "missed": max(0, expected - used) if known else None,
                     "stars": row["stars"], "triples": row["triples"], "destruction": row["destruction"],
                     "attacks_measured": row["measured"], "final": war["state"] == "warEnded",
                     "attacks_complete": coverage["own"]["attacks_complete"]})
    return rows


def first_gap(connection, war_id, attacks):
    """The first of Clash's attack orders that the record cannot place on a defender.

    Clash numbers both sides' attacks 1, 2, 3... in one sequence, so with both
    sides' attack counts known, a missing number is an unsaved attack. An attack
    saved without its defender is a gap too. Every attack before the first gap
    is saved with its defender. Without both counts no order is safe.
    """
    reported = [row[0] for row in connection.execute("SELECT attacks FROM war_sides WHERE war_id=?", (war_id,))]
    orders = {attack["attack_order"] for attack in attacks}
    if len(reported) != 2 or None in reported or any(not 1 <= order <= sum(reported) for order in orders):
        return 1
    gaps = set(range(1, sum(reported) + 1)) - orders
    gaps.update(attack["attack_order"] for attack in attacks
                if (attack["defender_tag"] or json.loads(attack["extra_json"]).get("defender_tag")) is None)
    return min(gaps, default=sum(reported) + 1)


def selection(time_field, states):
    if isinstance(time_field, (tuple, list)):
        if states is not None or not 1 <= len(time_field) <= 6:
            raise ValueError("Expected 1..6 time and state clauses")
        clauses = []
        for clause in time_field:
            if not isinstance(clause, (tuple, list)) or len(clause) != 2 or clause[0] not in TIME_FIELDS:
                raise ValueError("Expected time and state clauses")
            clauses.append(selection(*clause))
        return tuple(clauses), None
    if time_field not in TIME_FIELDS:
        raise ValueError("Expected end_at or observed_at")
    states = None if states is None else tuple(sorted(set(states)))
    if states is not None and (not states or any(state not in STATES for state in states)):
        raise ValueError("Expected preparation, inWar or warEnded states")
    return time_field, states


def select_wars(connection, clans, after, before, types, at, time_field="end_at", states=None):
    """Wars in an explicit half-open interval over stored end or last observation times."""
    if isinstance(time_field, tuple):
        selected = {}
        for field, selected_states in time_field:
            for item in select_wars(connection, clans, after, before, types, at, field, selected_states):
                selected.setdefault((item[0]["id"], item[1]), item)
        if len(selected) > MAX_WARS:
            raise ValueError(f"The window selects more than {MAX_WARS} wars; choose a shorter one")
        return sorted(selected.values(), key=lambda item: (-item[2], -item[0]["id"], item[1]))
    where = (f"s.clan_tag IN ({placeholders(clans)}) AND w.type IN ({placeholders(types)}) AND w.first_seen_at<=?")
    args = (*clans, *types, at)
    if states is not None:
        where += f" AND w.state IN ({placeholders(states)})"
        args += states
    # Source checks may observe an unchanged war without rewriting its facts.
    observed = ("MIN(?,MAX(w.last_seen_at,COALESCE((SELECT MAX(c.last_success_at) FROM war_collection_state c "
                "WHERE c.war_id=w.id AND ((c.source_kind='current_war' AND c.scope_id=s.clan_tag) "
                "OR (c.source_kind='cwl_war' AND c.scope_id=w.war_tag))),w.last_seen_at)))")
    column = "w.end_at" if time_field == "end_at" else observed
    rows = connection.execute(
        f"SELECT w.*,s.clan_tag AS facing,{column} AS selected_at FROM wars w JOIN war_sides s ON s.war_id=w.id "
        f"WHERE {where} AND selected_at>=? AND selected_at<? AND selected_at<=? LIMIT ?",
        (*(() if time_field == "end_at" else (at,)), *args, after, before, at, MAX_WARS + 1)).fetchall()
    if len(rows) > MAX_WARS:
        raise ValueError(f"The window selects more than {MAX_WARS} wars; choose a shorter one")
    return sorted(((row, row["facing"], row["selected_at"], time_field) for row in rows),
                  key=lambda item: (-item[2], -item[0]["id"], item[1]))


def window_coverage(connection, clans, types, after, before, at, wars, truncated):
    signals = [war_signal(connection, kind, clan, at) for clan in clans for kind in
               ("current_war", "war_log", *(("cwl_group",) if "CWL" in types else ()))]
    result = envelope(signals, after=after, before=before, at=at, truncated=truncated)
    incomplete = []
    for war, clan, coverage in wars:
        causes = [cause for cause, missing in (
            ("not_final", war["state"] != "warEnded"), ("lineup_incomplete", not coverage["own"]["lineup_complete"]),
            ("attacks_incomplete", not coverage["own"]["attacks_complete"])) if missing]
        if causes:
            incomplete.append({"war_id": war["id"], "clan_tag": clan, "causes": causes})
    result.update(types=list(types), wars_selected=len(wars), incomplete_wars=incomplete[:200])
    result["truncated"] |= len(incomplete) > 200
    result["complete"] = result["complete"] and not incomplete
    return result


def add(total, value):
    return None if total is None or value is None else total + value


def by_owner(connection, rows, at):
    """Group account rows by each account's owner at as_of; unlinked accounts stay apart."""
    owners = {tag: ownership(connection, tag, at) for tag in sorted({row["player_tag"] for row in rows})}
    groups = {}
    for row in rows:
        owner = owners[row["player_tag"]]
        key = ("member", owner["member_id"]) if owner["member_id"] else ("account", row["player_tag"])
        group = groups.setdefault(key, {
            "unit": key[0], "member_id": owner["member_id"], "player_tags": [], "ownership_status": owner["status"],
            "war_ids": [], "opportunities": 0, "attacks_used": 0, "attacks_expected": 0, "missed": 0,
            "stars": 0, "triples": 0, "destruction": 0, "attacks_measured": 0})
        if row["player_tag"] not in group["player_tags"]:
            group["player_tags"].append(row["player_tag"])
        if row["war_id"] not in group["war_ids"]:
            group["war_ids"].append(row["war_id"])
        group["opportunities"] += 1
        for field in ("attacks_used", "attacks_expected", "missed", "stars", "triples", "destruction", "attacks_measured"):
            group[field] = add(group[field], row[field])
    result = []
    for _, group in sorted(groups.items(), key=lambda item: item[0]):
        group["wars"] = len(group.pop("war_ids"))
        group["player_tags"].sort()
        group["totals_complete"] = group["missed"] is not None
        result.append(group)
    return result


class WarReads(RecordReads):
    """Bounded war reads; every result carries coverage and record war IDs.

    Window bounds and as_of are UTC epoch microseconds. Ownership grouping
    uses each account's owner at as_of, not the owner at war time.
    """

    def cache_revision(self):
        """The record's commit revision; any write, a war check included, changes it."""
        path = getattr(self.store, "path", None)
        return sqlite_revision(path) if path is not None else None

    async def current(self, clan, as_of=None, *, lineups=False):
        """What the war board shows now: the clan's latest current-war check.

        With lineups, both sides list their accounts and stored attacks.
        """
        return await self._read(self._current, clan_tags([clan])[0], timestamp(as_of), bool(lineups))

    @staticmethod
    def _current(connection, clan, at, lineups=False):
        signal = war_signal(connection, "current_war", clan, at)
        state = connection.execute("SELECT war_id FROM war_collection_state WHERE source_kind='current_war' "
                                   "AND scope_id=?", (clan,)).fetchone()
        observed = signal["observed"].get("state")
        war = None
        if observed not in (None, "notInWar") and state and state["war_id"]:
            row = connection.execute("SELECT * FROM wars WHERE id=?", (state["war_id"],)).fetchone()
            war = war_view(connection, row, clan, at, lineups) if row else None
        return {"clan_tag": clan, "state": observed, "checked_at": signal["last_attempt_at"],
                "observed_at": signal["last_good_at"], "war": war, "coverage": envelope([signal], at=at)}

    async def previous(self, clan, as_of=None, *, types=("FRIENDLY", "REG"), lineups=False):
        """The clan's last ended non-league war before the current one, as the board's Previous War."""
        return await self._read(self._previous, clan_tags([clan])[0], timestamp(as_of),
                                war_types(types, ("FRIENDLY", "REG")), bool(lineups))

    @staticmethod
    def _previous(connection, clan, at, types, lineups=False):
        signals = [war_signal(connection, kind, clan, at) for kind in ("current_war", "war_log")]
        state = connection.execute("SELECT w.* FROM war_collection_state s JOIN wars w ON w.id=s.war_id "
                                   "WHERE s.source_kind='current_war' AND s.scope_id=?", (clan,)).fetchone()
        before, exclude = at, None
        if state is not None and signals[0]["observed"].get("state") not in (None, "notInWar"):
            before, exclude = min(at, state["preparation_at"] or state["end_at"]), state["id"]
        row = connection.execute(
            f"SELECT w.* FROM wars w JOIN war_sides s ON s.war_id=w.id AND s.clan_tag=? WHERE w.war_tag IS NULL "
            f"AND w.type IN ({placeholders(types)}) AND w.first_seen_at<=? AND w.end_at<=? AND w.id IS NOT ? "
            "ORDER BY w.end_at DESC,w.id DESC LIMIT 1", (clan, *types, at, before, exclude)).fetchone()
        return {"clan_tag": clan, "war": war_view(connection, row, clan, at, lineups) if row else None,
                "coverage": envelope(signals, at=at)}

    async def completed(self, clans, after, before, *, types=REGULAR, as_of=None, limit=200, offset=0):
        """Wars ended in [after, before) with both scores, result and named missed attacks."""
        pagination(limit, offset)
        return await self._read(self._completed, clan_tags(clans), *window(after, before), war_types(types),
                                timestamp(as_of), limit, offset)

    @staticmethod
    def _completed(connection, clans, after, before, types, at, limit, offset):
        selected = select_wars(connection, clans, after, before, types, at)
        checked = [(war, clan, war_side_coverage(connection, war["id"], clan)) for war, clan, _, _ in selected]
        result = page(selected[offset:offset + limit + 1], limit, offset)
        rows = []
        for war, clan, _, _ in result["rows"]:
            view = war_view(connection, war, clan, at)
            members = member_rows(connection, war, clan, view["coverage"])
            known = bool(members) and all(row["missed"] is not None for row in members)
            view["missed_attacks"] = sorted(
                ({key: row[key] for key in ("player_tag", "player_name", "map_position", "attacks_used",
                                            "attacks_expected", "missed")} for row in members if row["missed"]),
                key=lambda row: (row["map_position"] is None, row["map_position"] or 0, row["player_tag"])) if known else None
            rows.append(view)
        result["rows"] = rows
        result["coverage"] = window_coverage(connection, clans, types, after, before, at, checked, result["truncated"])
        return result

    async def participation(self, clans, after, before, *, types=REGULAR, player_tags=None, group_by="account",
                            time_field="end_at", states=None, as_of=None, limit=200, offset=0):
        """Per account per war in wars ended in [after, before); or grouped by owner at as_of."""
        pagination(limit, offset)
        if group_by not in ("account", "owner"):
            raise ValueError("Expected account or owner grouping")
        selected = None if player_tags is None else tags(player_tags)
        return await self._read(self._participation, clan_tags(clans), *window(after, before), war_types(types),
                                selected, group_by, *selection(time_field, states), timestamp(as_of), limit, offset)

    @staticmethod
    def _participation(connection, clans, after, before, types, selected, group_by, time_field, states, at, limit, offset):
        rows, checked = [], []
        for war, clan, time, basis in select_wars(connection, clans, after, before, types, at, time_field, states):
            coverage = war_side_coverage(connection, war["id"], clan)
            checked.append((war, clan, coverage))
            rows.extend({**row, "state": war["state"], "time": time, "time_basis": basis}
                        for row in member_rows(connection, war, clan, coverage)
                        if selected is None or row["player_tag"] in selected)
        if group_by == "owner":
            rows = by_owner(connection, rows, at)
        result = page(rows[offset:offset + limit + 1], limit, offset)
        result.update(group_by=group_by, ownership_basis="owner at as_of" if group_by == "owner" else None,
                      coverage=window_coverage(connection, clans, types, after, before, at, checked, result["truncated"]))
        return result

    async def attacks(self, clans, after, before, *, types=REGULAR, player_tags=None, direction="attacks",
                      time_field="end_at", states=None, as_of=None, limit=200, offset=0):
        """Attack detail with both town halls; defenses are the opponent's attacks on the clan.

        An attack copied from old clan health, whose defender the record cannot
        place, shows the defender and first hit as the old capture saw them.
        """
        pagination(limit, offset)
        if direction not in ("attacks", "defenses"):
            raise ValueError("Expected attacks or defenses")
        selected = None if player_tags is None else tags(player_tags)
        return await self._read(self._attacks, clan_tags(clans), *window(after, before), war_types(types),
                                selected, direction, *selection(time_field, states), timestamp(as_of), limit, offset)

    @staticmethod
    def _attacks(connection, clans, after, before, types, selected, direction, time_field, states, at, limit, offset):
        rows, checked, lineups = [], [], []
        for war, clan, time, basis in select_wars(connection, clans, after, before, types, at, time_field, states):
            coverage = war_side_coverage(connection, war["id"], clan)
            checked.append((war, clan, coverage))
            own = side_of(connection, war["id"], clan)
            complete = {own: coverage["own"], 1 - own: coverage["opponent"]}
            side_lineups = {side: [dict(row) for row in connection.execute(
                "SELECT player_tag,source_map_position AS map_position FROM war_members WHERE war_id=? AND side=? "
                "ORDER BY source_map_position IS NULL,source_map_position,player_tag", (war["id"], side))]
                for side in (0, 1)}
            lineups.append({"war_id": war["id"], "clan_tag": clan, "coverage": coverage,
                            "members": side_lineups[1 - own if direction == "attacks" else own]})
            places = {side: {m["player_tag"]: m["map_position"] for m in side_lineups[side]}
                      if complete[side]["lineup_complete"] else {} for side in (0, 1)}
            attacks = connection.execute(
                "SELECT a.*,x.player_tag AS attacker_tag,x.name AS attacker_name,x.townhall AS attacker_townhall,"
                "x.source_map_position AS attacker_map_position,x.side AS attacker_side,d.player_tag AS defender_tag,"
                "d.name AS defender_name,d.townhall AS defender_townhall,d.source_map_position AS defender_map_position,"
                "d.side AS defender_side,d.extra_json AS defender_extra FROM war_attacks a "
                "LEFT JOIN war_members x ON x.id=a.attacker_member_id "
                "LEFT JOIN war_members d ON d.id=a.defender_member_id WHERE a.war_id=? ORDER BY a.attack_order",
                (war["id"],)).fetchall()
            first = {}
            for attack in attacks:
                extra = json.loads(attack["extra_json"])
                defender = attack["defender_tag"] or extra.get("defender_tag")
                first.setdefault(defender, attack["attack_order"])
            gap = first_gap(connection, war["id"], attacks)
            for attack in attacks:
                extra = json.loads(attack["extra_json"])
                # A known defender places an unattributed attacker on the other side.
                side = (attack["attacker_side"] if attack["attacker_side"] is not None else
                        1 - attack["defender_side"] if attack["defender_side"] is not None else None)
                if extra.get("stub") or side is None or (side == own) != (direction == "attacks"):
                    continue
                attacker = attack["attacker_tag"] or extra.get("attacker_tag")
                defender = attack["defender_tag"] or extra.get("defender_tag")
                if selected is not None and (attacker if direction == "attacks" else defender) not in selected:
                    continue
                # Freshness needs every earlier attack on that defender: all the side's attacks,
                # or every attack before this one by Clash's order.
                fresh = (False if first[defender] < attack["attack_order"] else
                         True if complete[side]["attacks_complete"] or attack["attack_order"] < gap else None)
                # Old clan health kept only its own lineup, so a copied attack's defender may be
                # missing or incomplete in the record; the old capture's view of the defender and
                # of a first hit is kept with the attack and fills what the record does not know.
                legacy = extra.get("legacy") if isinstance(extra.get("legacy"), dict) else {}
                known = {"defender_name": attack["defender_name"], "defender_townhall": attack["defender_townhall"],
                         "defender_position": places[1 - side].get(defender)}
                # A league base copied from old clan health is known only from the attacks on it: its
                # place among the bases that were hit means nothing, so it keeps the old capture's position.
                base = json.loads(attack["defender_extra"]).get("legacy") if attack["defender_extra"] else None
                copied = isinstance(base, dict) and bool(base.get("from_attacks"))
                if copied:
                    known["defender_position"] = None
                filled = {key: legacy.get(key) for key, value in known.items() if value is None and legacy.get(key)}
                if copied and attack["defender_map_position"] is not None:
                    filled["defender_position"] = attack["defender_map_position"]
                rows.append({"war_id": war["id"], "clan_tag": clan, "type": war["type"], "state": war["state"],
                             "time": time, "time_basis": basis, "attack_order": attack["attack_order"],
                             "attacker_tag": attacker, "attacker_name": attack["attacker_name"],
                             "attacker_townhall": attack["attacker_townhall"],
                             "attacker_map_position": attack["attacker_map_position"],
                             "defender_tag": defender, **known, **filled,
                             "defender_map_position": attack["defender_map_position"],
                             "defender_basis": "old_capture" if filled else "lineup",
                             "stars": attack["stars"], "destruction": attack["destruction"],
                             "duration": attack["duration"],
                             "fresh": fresh if fresh is not None else legacy.get("fresh_attack"),
                             "fresh_basis": "old_capture" if fresh is None and "fresh_attack" in legacy else "attacks"})
        result = page(rows[offset:offset + limit + 1], limit, offset)
        facing = {(row["war_id"], row["clan_tag"]) for row in result["rows"]}
        result.update(direction=direction, time_field=time_field,
                      lineups=[war for war in lineups if (war["war_id"], war["clan_tag"]) in facing],
                      coverage=window_coverage(connection, clans, types, after, before, at, checked, result["truncated"]))
        return result

    async def log_entries(self, clan, *, as_of=None, limit=200, offset=0):
        """Stored war-log entries, newest first; a league summary is a single entry."""
        pagination(limit, offset)
        return await self._read(self._log_entries, clan_tags([clan])[0], timestamp(as_of), limit, offset)

    @staticmethod
    def _log_entries(connection, clan, at, limit, offset):
        rows = [dict(row) for row in connection.execute(
            "SELECT w.id AS war_id,w.end_at,s.result,'war' AS kind FROM wars w JOIN war_sides s "
            "ON s.war_id=w.id AND s.clan_tag=? WHERE w.first_seen_at<=? AND w.end_at<=? "
            "AND EXISTS (SELECT 1 FROM war_source_keys k WHERE k.war_id=w.id AND k.source_kind='war_log') "
            "UNION ALL SELECT NULL,end_at,result,'league_week' FROM cwl_log_summaries WHERE clan_tag=? AND end_at<=? "
            "ORDER BY end_at DESC,war_id DESC LIMIT ? OFFSET ?", (clan, at, at, clan, at, limit + 1, offset))]
        signal = war_signal(connection, "war_log", clan, at)
        status = ("private_log" if signal["status"] == "private" else
                  "unavailable" if signal["last_good_at"] is None else "available")
        result = page(rows, limit, offset)
        result.update(clan_tag=clan, status=status, coverage=envelope([signal], at=at, truncated=result["truncated"]))
        return result

    async def totals(self, clans, after, before, *, player_tags, time_field="end_at", states=None, types=REGULAR, as_of=None):
        """Each selected account's neutral totals over explicitly selected wars.

        One row per account found in a lineup: its wars, the expected and used
        attacks of the wars that know both, the wars that do not, and its
        stored attacks with their stars, triples and destruction. At most 200
        accounts, so one read covers a window no page could.
        """
        return await self._read(self._totals, clan_tags(clans), *window(after, before),
                                war_types(types), clan_tags(player_tags), *selection(time_field, states), timestamp(as_of))

    @staticmethod
    def _totals(connection, clans, after, before, types, selected, time_field, states, at):
        totals, checked = {}, []
        for war, clan, _, _ in select_wars(connection, clans, after, before, types, at, time_field, states):
            # A war without a selected account in the clan's lineup adds nothing.
            if not connection.execute(
                    f"SELECT 1 FROM war_members WHERE war_id=? AND side=? AND player_tag IN ({placeholders(selected)}) "
                    "LIMIT 1", (war["id"], side_of(connection, war["id"], clan), *selected)).fetchone():
                continue
            coverage = war_side_coverage(connection, war["id"], clan)
            checked.append((war, clan, coverage))
            for row in member_rows(connection, war, clan, coverage):
                if row["player_tag"] not in selected:
                    continue
                total = totals.setdefault(row["player_tag"], {
                    "player_tag": row["player_tag"], "wars": 0, "wars_without_counts": 0, "attacks_expected": 0,
                    "attacks_used": 0, "stars": 0, "triples": 0, "destruction": 0, "attacks_measured": 0})
                total["wars"] += 1
                if row["attacks_expected"] is None or row["attacks_used"] is None:
                    total["wars_without_counts"] += 1
                else:
                    total["attacks_expected"] += row["attacks_expected"]
                    total["attacks_used"] += row["attacks_used"]
                for field in ("stars", "triples", "destruction", "attacks_measured"):
                    total[field] += row[field]
        return {"rows": [totals[tag] for tag in sorted(totals)], "time_field": time_field,
                "coverage": window_coverage(connection, clans, types, after, before, at, checked, False)}

    async def counts(self, clans, after, before, *, time_field="end_at", states=None, types=REGULAR, as_of=None):
        """Per clan, selected wars and how many have a stored lineup."""
        return await self._read(self._counts, clan_tags(clans), *window(after, before),
                                war_types(types), *selection(time_field, states), timestamp(as_of))

    @staticmethod
    def _counts(connection, clans, after, before, types, time_field, states, at):
        counts = {clan: {"wars": 0, "wars_with_lineup": 0} for clan in clans}
        checked = []
        for war, clan, _, _ in select_wars(connection, clans, after, before, types, at, time_field, states):
            coverage = war_side_coverage(connection, war["id"], clan)
            checked.append((war, clan, coverage))
            counts[clan]["wars"] += 1
            counts[clan]["wars_with_lineup"] += coverage["own"]["members"] > 0
        return {"clans": counts, "time_field": time_field,
                "coverage": window_coverage(connection, clans, types, after, before, at, checked, False)}
