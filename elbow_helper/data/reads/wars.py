"""Clan-facing war reads: boards, completed wars, participation, attacks and log counts.

Every war is shown facing the requested clan. Regular wars are the default;
friendly and league wars are read only when asked for, and each row keeps
its type and what decided it. A count the record cannot prove stays None:
an incomplete lineup or attack list never becomes a zero.
"""

from datetime import datetime, timezone
import json

from elbow_helper.infrastructure.persistence.revisions import sqlite_revision

from .core import RecordReads, page, pagination, placeholders, tags, timestamp
from .coverage import SECOND, envelope, war_side_coverage, war_signal
from .people import ownership

TYPES = ("CWL", "FRIENDLY", "REG")
REGULAR = ("REG",)
# Old clan health keeps two meanings: wars as last captured, ongoing ones
# included, with an inclusive end; or ended wars with an exclusive end.
VIEWS = ("completed", "capture")
# The monthly summary counts the newest 50 entries of Clash's war log.
LOG_WINDOW = 50
# A window may select at most this many clan-facing wars.
MAX_WARS = 1000
RESULTS = {"win": "wins", "lose": "losses", "tie": "ties"}


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


def month_bounds(month):
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise ValueError("Expected a month as YYYY-MM") from None
    if start.strftime("%Y-%m") != month:
        raise ValueError("Expected a month as YYYY-MM")
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return int(start.timestamp()) * SECOND, int(end.timestamp()) * SECOND


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


def positions(connection, war_id, side, complete):
    """Map positions renumbered 1..n, as old clan health shows them; only for a complete lineup."""
    if not complete:
        return {}
    members = connection.execute("SELECT player_tag FROM war_members WHERE war_id=? AND side=? "
                                 "ORDER BY source_map_position,player_tag", (war_id, side)).fetchall()
    return {row[0]: index for index, row in enumerate(members, 1)}


def capture_time(war, current, at):
    """When a war was last known as captured: its end once ended, else its last check."""
    if war["state"] == "warEnded":
        return war["end_at"], "war_end"
    checked = war["last_seen_at"]
    if current and current["war_id"] == war["id"] and current["last_success_at"] is not None:
        checked = max(checked, current["last_success_at"])
    return min(at, checked), "last_check"


def select_wars(connection, clans, after, before, types, at, view):
    """Clan-facing wars in a window, newest first, as (war, clan, time, time_basis)."""
    where = (f"s.clan_tag IN ({placeholders(clans)}) AND w.type IN ({placeholders(types)}) AND w.first_seen_at<=?")
    args = (*clans, *types, at)
    if view == "completed":
        rows = connection.execute(
            f"SELECT w.*,s.clan_tag AS facing FROM wars w JOIN war_sides s ON s.war_id=w.id WHERE {where} "
            "AND w.end_at>=? AND w.end_at<? AND w.end_at<=? LIMIT ?", (*args, after, before, at, MAX_WARS + 1)).fetchall()
        selected = [(row, row["facing"], row["end_at"], "war_end") for row in rows]
    else:
        current = {row["scope_id"]: row for row in connection.execute(
            f"SELECT scope_id,war_id,last_success_at FROM war_collection_state WHERE source_kind='current_war' "
            f"AND scope_id IN ({placeholders(clans)})", clans)}
        rows = connection.execute(
            f"SELECT w.*,s.clan_tag AS facing FROM wars w JOIN war_sides s ON s.war_id=w.id WHERE {where} "
            "AND (w.state!='warEnded' OR w.end_at BETWEEN ? AND ?)", (*args, after, before)).fetchall()
        selected = [(row, row["facing"], *capture_time(row, current.get(row["facing"]), at)) for row in rows]
        selected = [item for item in selected if after <= item[2] <= before]
    if len(selected) > MAX_WARS:
        raise ValueError(f"The window selects more than {MAX_WARS} wars; choose a shorter one")
    return sorted(selected, key=lambda item: (-item[2], -item[0]["id"], item[1]))


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
        selected = select_wars(connection, clans, after, before, types, at, "completed")
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
                            as_of=None, limit=200, offset=0):
        """Per account per war in wars ended in [after, before); or grouped by owner at as_of."""
        pagination(limit, offset)
        if group_by not in ("account", "owner"):
            raise ValueError("Expected account or owner grouping")
        selected = None if player_tags is None else tags(player_tags)
        return await self._read(self._participation, clan_tags(clans), *window(after, before), war_types(types),
                                selected, group_by, timestamp(as_of), limit, offset)

    @staticmethod
    def _participation(connection, clans, after, before, types, selected, group_by, at, limit, offset):
        rows, checked = [], []
        for war, clan, _, _ in select_wars(connection, clans, after, before, types, at, "completed"):
            coverage = war_side_coverage(connection, war["id"], clan)
            checked.append((war, clan, coverage))
            rows.extend(row for row in member_rows(connection, war, clan, coverage)
                        if selected is None or row["player_tag"] in selected)
        if group_by == "owner":
            rows = by_owner(connection, rows, at)
        result = page(rows[offset:offset + limit + 1], limit, offset)
        result.update(group_by=group_by, ownership_basis="owner at as_of" if group_by == "owner" else None,
                      coverage=window_coverage(connection, clans, types, after, before, at, checked, result["truncated"]))
        return result

    async def attacks(self, clans, after, before, *, types=REGULAR, player_tags=None, direction="attacks",
                      view="completed", as_of=None, limit=200, offset=0):
        """Attack detail with both town halls; defenses are the opponent's attacks on the clan."""
        pagination(limit, offset)
        if direction not in ("attacks", "defenses") or view not in VIEWS:
            raise ValueError("Expected attacks or defenses and a completed or capture view")
        selected = None if player_tags is None else tags(player_tags)
        return await self._read(self._attacks, clan_tags(clans), *window(after, before), war_types(types),
                                selected, direction, view, timestamp(as_of), limit, offset)

    @staticmethod
    def _attacks(connection, clans, after, before, types, selected, direction, view, at, limit, offset):
        rows, checked = [], []
        for war, clan, time, basis in select_wars(connection, clans, after, before, types, at, view):
            coverage = war_side_coverage(connection, war["id"], clan)
            checked.append((war, clan, coverage))
            own = side_of(connection, war["id"], clan)
            complete = {own: coverage["own"], 1 - own: coverage["opponent"]}
            places = {side: positions(connection, war["id"], side, complete[side]["lineup_complete"]) for side in (0, 1)}
            attacks = connection.execute(
                "SELECT a.*,x.player_tag AS attacker_tag,x.name AS attacker_name,x.townhall AS attacker_townhall,"
                "x.source_map_position AS attacker_map_position,x.side AS attacker_side,d.player_tag AS defender_tag,"
                "d.name AS defender_name,d.townhall AS defender_townhall,d.source_map_position AS defender_map_position,"
                "d.side AS defender_side FROM war_attacks a LEFT JOIN war_members x ON x.id=a.attacker_member_id "
                "LEFT JOIN war_members d ON d.id=a.defender_member_id WHERE a.war_id=? ORDER BY a.attack_order",
                (war["id"],)).fetchall()
            first = {}
            for attack in attacks:
                extra = json.loads(attack["extra_json"])
                defender = attack["defender_tag"] or extra.get("defender_tag")
                first.setdefault(defender, attack["attack_order"])
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
                # Freshness needs every earlier attack on that defender.
                fresh = (False if first[defender] < attack["attack_order"] else
                         True if complete[side]["attacks_complete"] else None)
                rows.append({"war_id": war["id"], "clan_tag": clan, "type": war["type"], "state": war["state"],
                             "time": time, "time_basis": basis, "attack_order": attack["attack_order"],
                             "attacker_tag": attacker, "attacker_name": attack["attacker_name"],
                             "attacker_townhall": attack["attacker_townhall"],
                             "attacker_map_position": attack["attacker_map_position"],
                             "defender_tag": defender, "defender_name": attack["defender_name"],
                             "defender_townhall": attack["defender_townhall"],
                             "defender_map_position": attack["defender_map_position"],
                             "defender_position": places[1 - side].get(defender),
                             "stars": attack["stars"], "destruction": attack["destruction"],
                             "duration": attack["duration"], "fresh": fresh})
        result = page(rows[offset:offset + limit + 1], limit, offset)
        result.update(direction=direction, view=view,
                      coverage=window_coverage(connection, clans, types, after, before, at, checked, result["truncated"]))
        return result

    async def log_results(self, clan, month, as_of=None):
        """Wins, losses and ties as the monthly summary counts the war log's newest 50 entries.

        A league week's single log entry counts once. Older recorded entries of
        the month are counted apart, never mixed into the old meaning.
        """
        return await self._read(self._log_results, clan_tags([clan])[0], month_bounds(month), month, timestamp(as_of))

    @staticmethod
    def _log_results(connection, clan, bounds, month, at):
        start, end = bounds
        entries = [(row["end_at"], row["id"], row["result"], "war") for row in connection.execute(
            "SELECT w.id,w.end_at,s.result FROM wars w JOIN war_sides s ON s.war_id=w.id AND s.clan_tag=? "
            "WHERE w.first_seen_at<=? AND w.end_at<=? AND EXISTS (SELECT 1 FROM war_source_keys k "
            "WHERE k.war_id=w.id AND k.source_kind='war_log')", (clan, at, at))]
        entries += [(row["end_at"], None, row["result"], "league_week") for row in connection.execute(
            "SELECT end_at,result FROM cwl_log_summaries WHERE clan_tag=? AND end_at<=?", (clan, at))]
        entries.sort(key=lambda entry: (entry[0], entry[1] or 0), reverse=True)

        def counts(selected):
            totals = {"wins": 0, "losses": 0, "ties": 0, "unknown_result": 0}
            for entry in selected:
                totals[RESULTS.get(entry[2], "unknown_result")] += 1
            totals["total"] = totals["wins"] + totals["losses"] + totals["ties"]
            return totals

        recent = [entry for entry in entries[:LOG_WINDOW] if start <= entry[0] < end]
        older = [entry for entry in entries[LOG_WINDOW:] if start <= entry[0] < end]
        signal = war_signal(connection, "war_log", clan, at)
        status = ("private_log" if signal["status"] == "private" else
                  "unavailable" if signal["last_good_at"] is None else "available")
        coverage = envelope([signal], after=start, before=end, at=at)
        # Entries of the month are all in the log only once it was read after the month ended.
        coverage["read_after_month"] = signal["last_good_at"] is not None and signal["last_good_at"] >= end
        coverage["complete"] = (coverage["complete"] and coverage["read_after_month"]
                                and not signal["log_limits"]["backfill_pending"])
        available = status == "available"
        return {"clan_tag": clan, "month": month, "after": start, "before": end, "status": status,
                **(counts(recent) if available else dict.fromkeys(("wins", "losses", "ties", "unknown_result", "total"))),
                "entries": [{"war_id": war_id, "end_at": at_end, "result": result, "kind": kind}
                            for at_end, war_id, result, kind in recent] if available else [],
                "beyond_log_window": counts(older) if available else None, "coverage": coverage}

    async def health_activity(self, clans, after, before, *, view="capture", types=REGULAR, player_tags=None,
                              as_of=None, limit=200, offset=0):
        """Regular-war player health inputs in old clan health's two named views.

        capture: every war as last captured, an ongoing one at its last check,
        in [after, before]. completed: wars ended in [after, before).
        """
        pagination(limit, offset)
        if view not in VIEWS:
            raise ValueError("Expected a completed or capture view")
        selected = None if player_tags is None else tags(player_tags)
        return await self._read(self._health_activity, clan_tags(clans), *window(after, before),
                                war_types(types, ("FRIENDLY", "REG")), selected, view, timestamp(as_of), limit, offset)

    @staticmethod
    def _health_activity(connection, clans, after, before, types, selected, view, at, limit, offset):
        rows, checked = [], []
        for war, clan, time, basis in select_wars(connection, clans, after, before, types, at, view):
            coverage = war_side_coverage(connection, war["id"], clan)
            checked.append((war, clan, coverage))
            for row in member_rows(connection, war, clan, coverage):
                if selected is None or row["player_tag"] in selected:
                    rows.append({**{key: value for key, value in row.items() if key not in ("missed", "end_at")},
                                 "type_basis": war["type_basis"], "state": war["state"],
                                 "time": time, "time_basis": basis, "end_at": war["end_at"]})
        result = page(rows[offset:offset + limit + 1], limit, offset)
        result.update(view=view, bounds="inclusive" if view == "capture" else "exclusive_end",
                      coverage=window_coverage(connection, clans, types, after, before, at, checked, result["truncated"]))
        return result
