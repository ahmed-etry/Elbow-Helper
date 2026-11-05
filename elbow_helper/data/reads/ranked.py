"""Stored ranked battles per account, member or clan, and where each battle log went unwatched.

Battles are the record's own: live battle-log polls and the startup copy of the
old ranked store. Ranked means battle type ranked or legend, in any case. A
window is [after, before) in UTC epoch microseconds and ``as_of`` bounds what
the record knew of ownership, clans, profiles and watching; battles are not
dated by when the record stored them, since copied ones predate the copy. Watch coverage comes from the compact per-watcher facts; a gap
says the log was not read cleanly there, not that battles were lost. Battles
before an account's first clean read are the tail that read still held.
"""

from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
import json

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.domain.ranked import RANKED_MODES
from elbow_helper.infrastructure.persistence.revisions import sqlite_revision

from ..store.sources import INTERVALS
from .core import RecordReads, placeholders, timestamp
from .coverage import signal
from .profile_history import counter_values

SELECTORS = ("member_ids", "account_tags", "clan_tags")
# One request can name a whole retained cohort; larger selections are refused.
MAX_SELECTION = 10_000
CADENCE = INTERVALS["battle_log"]
RANKING_COUNTERS = ("trophies", "bestTrophies", "attackWins", "defenseWins")


def selection(selector, values):
    if selector not in SELECTORS:
        raise ValueError("Expected member_ids, account_tags or clan_tags")
    values = tuple(values)
    if not 1 <= len(values) <= MAX_SELECTION:
        raise ValueError("Expected 1..10000 selections")
    if selector == "member_ids":
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("Expected positive member IDs")
        return tuple(dict.fromkeys(values))
    tags = tuple(normalize_player_tag(value) for value in values)
    if any(tag is None for tag in tags):
        raise ValueError("Invalid Clash tag")
    return tuple(dict.fromkeys(tags))


def day(value):
    return datetime.fromtimestamp(value / 1_000_000, timezone.utc).date().isoformat()


def overlaps(start, end, after, before):
    return start < before and (end is None or end > after)


def clipped(gaps, after, before):
    result = []
    for gap in gaps:
        start, end = max(gap["after"], after), min(gap["before"], before)
        if start < end:
            result.append({**gap, "after": start, "before": end})
    return sorted(result, key=lambda gap: (gap["after"], gap["before"], gap["cause"]))


def watch_coverage(connection, tag, after, before, at):
    """Where the log was and wasn't read cleanly in [after, before), from both watchers."""
    end = min(before, at)
    watchers = [dict(row) for row in connection.execute(
        "SELECT * FROM battle_log_coverage WHERE player_tag=? AND first_attempt_at<=? ORDER BY origin", (tag, at))]
    gaps, failed, skipped, polls = [], 0, 0, 0
    clean = [row for row in watchers if row["first_clean_at"] is not None and row["first_clean_at"] <= at]
    for row in watchers:
        polls += row["polls"]
        skipped += row["skipped_entries"]
        others = [other for other in clean if other is not row]
        for span in json.loads(row["failed_spans_json"]):
            if span["from_at"] > at:
                continue
            start = span["previous_clean_at"] if span["previous_clean_at"] is not None else span["from_at"]
            stop = span["next_clean_at"] if span["next_clean_at"] is not None and span["next_clean_at"] <= at else end
            # Another watcher reading cleanly across the whole span covers it.
            if any(other["first_clean_at"] <= start and other["last_clean_at"] >= stop
                   and not any(overlaps(s["from_at"], s["next_clean_at"], start, stop)
                               for s in json.loads(other["failed_spans_json"]))
                   for other in others):
                continue
            if overlaps(start, stop, after, end):
                failed += span["polls"]
            gaps.append({"after": start, "before": stop, "cause": "log_not_read",
                         "reasons": dict(sorted(span["reasons"].items()))})
    first = min((row["first_clean_at"] for row in clean), default=None)
    last = max((row["last_clean_at"] for row in clean if row["last_clean_at"] <= at), default=None)
    if first is None:
        gaps = [{"after": after, "before": end, "cause": "never_read"}]
    else:
        gaps.insert(0, {"after": after, "before": first, "cause": "before_first_read"})
        ordered = sorted(clean, key=lambda row: row["first_clean_at"])
        for earlier, later in zip(ordered, ordered[1:]):
            if earlier["last_clean_at"] < later["first_clean_at"]:
                gaps.append({"after": earlier["last_clean_at"], "before": later["first_clean_at"],
                             "cause": "between_watchers"})
        if last is not None and last < end:
            gaps.append({"after": last, "before": end,
                         "cause": "not_yet_read" if at - last <= 2 * CADENCE else "overdue"})
    gaps = clipped(gaps, after, end)
    status = ("never_read" if first is None or first >= end else "partial" if gaps else "watched")
    return {"status": status, "watched_from": first, "last_read_at": last, "gaps": gaps,
            "failed_reads_in_window": failed, "skipped_entries": skipped, "reads": polls,
            "watchers": [row["origin"] for row in watchers], "continuous_state_known": False}


def ranking(connection, account_id, at):
    if account_id is None:
        return {}, None
    fields, observed, _ = counter_values(connection, account_id, at)
    result = {key: fields[key] for key in RANKING_COUNTERS if key in fields}
    times = [observed[key] for key in result]
    if "legendStatistics.fast" in fields:
        result["legendStatistics"] = fields["legendStatistics.fast"]
        times.append(observed["legendStatistics.fast"])
    for key in ("leagueTier", "league"):
        row = connection.execute(
            "SELECT value_json,observed_at FROM player_observations WHERE account_id=? AND field_key=? "
            "AND observed_at<=? ORDER BY observed_at DESC LIMIT 1", (account_id, key, at)).fetchone()
        if row:
            value = json.loads(row[0])
            if isinstance(value, dict):
                result[key] = {name: value[name] for name in ("id", "name") if name in value}
                times.append(row[1])
    return result, max(times, default=None)


def memberships(connection, account_id, at):
    if account_id is None:
        return []
    return [dict(row) for row in connection.execute(
        "SELECT c.tag,m.from_at,m.to_at FROM clan_membership m JOIN clans c ON c.id=m.clan_id "
        "WHERE m.account_id=? AND m.from_at<=? ORDER BY m.from_at", (account_id, at))]


def clan_at(stays, starts, when):
    index = bisect_right(starts, when) - 1
    while index >= 0:
        stay = stays[index]
        if stay["to_at"] is None or stay["to_at"] > when:
            return stay["tag"]
        index -= 1
    return None


def battles(connection, tag, after, before, at):
    rows = connection.execute(
        "SELECT b.*,t.value AS mode,o.value AS opponent,n.value AS opponent_name FROM family_battles b "
        "JOIN battle_strings s ON s.id=b.account_string_id JOIN battle_strings o ON o.id=b.opponent_string_id "
        "JOIN battle_strings t ON t.id=b.battle_type_id LEFT JOIN battle_strings n ON n.id=b.opponent_name_id "
        "WHERE s.value=? AND b.battle_at>=? AND b.battle_at<? ORDER BY b.battle_at,b.id", (tag, after, min(before, at)))
    result = []
    for row in rows:
        if row["mode"].upper() not in RANKED_MODES:
            continue
        payload = {"battleTimestamp": row["timestamp_text"], "battleType": row["mode"], "attack": bool(row["is_attack"])}
        if row["opponent"].startswith("#"):
            payload["opponentPlayerTag"] = row["opponent"]
        for key, column in (("opponentName", "opponent_name"), ("stars", "stars"),
                            ("destructionPercentage", "destruction_percentage"),
                            ("opponentTownHallLevel", "opponent_town_hall")):
            if row[column] is not None:
                payload[key] = row[column]
        legacy = json.loads(row["legacy_first_seen_json"]) if row["legacy_first_seen_json"] else None
        result.append({"battle_at": row["battle_at"], "attack": bool(row["is_attack"]), "payload": payload,
                       "clan_when_first_seen": legacy["clan_when_first_seen"] if legacy else None,
                       "legacy": legacy is not None})
    return result


def roster_starts(connection, clans, at):
    return {clan: signal(connection, "clan_roster", clan, at)["collection_start"] for clan in clans}


def in_clan(battle, clan, start, stays, starts):
    """Record membership where the record watched the roster; the old first-seen clan before that."""
    if start is not None and battle["battle_at"] >= start:
        return clan_at(stays, starts, battle["battle_at"]) == clan
    return battle["clan_when_first_seen"] == clan


def select_accounts(connection, selector, values, after, before, at):
    """Account tags per selection value, and the values nothing matched."""
    chosen = defaultdict(set)
    if selector == "account_tags":
        for tag in values:
            chosen[tag].add(tag)
    elif selector == "member_ids":
        for row in connection.execute(
                "SELECT m.discord_id,a.player_tag FROM account_links l JOIN accounts a ON a.id=l.account_id "
                f"JOIN members m ON m.id=l.member_id WHERE m.discord_id IN ({placeholders(values)}) "
                "AND l.from_at<=? AND (l.to_at IS NULL OR l.to_at>?)", (*values, at, at)):
            chosen[row["player_tag"]].add(row["discord_id"])
    else:
        end = min(before, at)
        for row in connection.execute(
                "SELECT c.tag,a.player_tag FROM clan_membership m JOIN clans c ON c.id=m.clan_id "
                f"JOIN accounts a ON a.id=m.account_id WHERE c.tag IN ({placeholders(values)}) "
                "AND m.from_at<? AND (m.to_at IS NULL OR m.to_at>?)", (*values, end, after)):
            chosen[row["player_tag"]].add(row["tag"])
        for row in connection.execute(
                "SELECT DISTINCT json_extract(b.legacy_first_seen_json,'$.clan_when_first_seen') AS clan,s.value AS tag "
                "FROM family_battles b JOIN battle_strings s ON s.id=b.account_string_id "
                "WHERE b.legacy_first_seen_json IS NOT NULL AND b.battle_at>=? AND b.battle_at<? "
                f"AND json_extract(b.legacy_first_seen_json,'$.clan_when_first_seen') IN ({placeholders(values)})",
                (after, before, *values)):
            chosen[row["tag"]].add(row["clan"])
    matched = {value for found in chosen.values() for value in found}
    return chosen, [value for value in values if value not in matched]


def account_row(connection, tag, chosen, selector, after, before, at, starts, include_battles):
    account = connection.execute("SELECT id FROM accounts WHERE player_tag=? AND first_observed_at<=?",
                                 (tag, at)).fetchone()
    identifier = account["id"] if account else None
    owner = connection.execute(
        "SELECT m.discord_id FROM account_links l JOIN members m ON m.id=l.member_id "
        "WHERE l.account_id=? AND l.from_at<=? AND (l.to_at IS NULL OR l.to_at>?)", (identifier, at, at)).fetchone()
    name = connection.execute("SELECT name FROM account_aliases WHERE account_id=? AND from_at<=? "
                              "ORDER BY from_at DESC LIMIT 1", (identifier, at)).fetchone()
    stays = memberships(connection, identifier, at)
    stay_starts = [stay["from_at"] for stay in stays]
    point = min(before, at) - 1
    found = battles(connection, tag, after, before, at)
    for battle in found:
        current = clan_at(stays, stay_starts, battle["battle_at"])
        battle["clan_tag"], battle["clan_basis"] = (
            (current, "record_membership") if current else
            (battle["clan_when_first_seen"], "clan_when_first_seen") if battle["legacy"] else (None, "unknown"))
    if selector == "clan_tags":
        found = [battle for battle in found if any(
            in_clan(battle, clan, starts[clan], stays, stay_starts) for clan in chosen)]
    coverage = watch_coverage(connection, tag, after, before, at)
    daily = defaultdict(lambda: [0, 0])
    for battle in found:
        daily[day(battle["battle_at"])][0 if battle["attack"] else 1] += 1
    values, observed_at = ranking(connection, identifier, point)
    known = bool(found) or coverage["status"] != "never_read"
    row = {"player_tag": tag, "member_id": owner[0] if owner else None, "player_name": name[0] if name else None,
           "clan_tag": clan_at(stays, stay_starts, point), "status": "ok" if observed_at is not None else "unavailable",
           "selection_rank": None, "ranking": values, "ranking_observed_at": observed_at,
           "attacks_used": sum(battle["attack"] for battle in found) if known else None,
           "defenses": sum(not battle["attack"] for battle in found) if known else None,
           "daily": [{"date": date, "attacks_used": attacks, "defenses": defenses}
                     for date, (attacks, defenses) in sorted(daily.items())],
           "battle_records": len(found), "old_store_battles": sum(battle["legacy"] for battle in found),
           "counts_complete": False, "log_coverage": coverage,
           "selected_by": sorted(chosen, key=str)}
    if include_battles:
        row["battles"] = [{"battle_at": battle["battle_at"], "clan_tag": battle["clan_tag"],
                           "clan_basis": battle["clan_basis"], "old_store": battle["legacy"],
                           "payload": battle["payload"]} for battle in found]
    return row


def window(connection, selector, values, after, before, at, offset, limit, include_battles):
    chosen, unmatched = select_accounts(connection, selector, values, after, before, at)
    starts = roster_starts(connection, values, at) if selector == "clan_tags" else {}
    rows = [account_row(connection, tag, chosen[tag], selector, after, before, at, starts, include_battles)
            for tag in sorted(chosen)]
    rows.sort(key=lambda row: (row["ranking"].get("trophies") is None,
                               -row["ranking"].get("trophies", 0), row["player_tag"]))
    last, rank = None, None
    for index, row in enumerate(rows):
        trophies = row["ranking"].get("trophies")
        if trophies is not None:
            if trophies != last:
                rank = index + 1
            row["selection_rank"], last = rank, trophies
    page = rows[offset:offset + limit]
    return {"after": after, "before": before, "as_of": at, "selector": selector, "accounts": page,
            "total_accounts": len(rows), "offset": offset,
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "unobserved_selections": unmatched,
            "coverage_complete": bool(rows) and all(not row["log_coverage"]["gaps"] for row in rows),
            "bases": {"modes": sorted(RANKED_MODES), "days": "battle time, UTC date",
                      "owner": "bot link at as_of", "ranking": "latest stored profile before the window end",
                      "clan": "record clan membership where the roster was watched, else the clan when the "
                              "old collector first saw the battle"}}


class RankedReads(RecordReads):
    """Ranked battle counts and watch coverage; never asks Clash."""

    def cache_revision(self):
        path = getattr(self.store, "path", None)
        return sqlite_revision(path) if path is not None else None

    async def window(self, selector, values, *, after, before, as_of=None, offset=0, limit=200,
                     include_battles=False):
        values = selection(selector, values)
        after, before, at = timestamp(after), timestamp(before), timestamp(as_of)
        if before <= after:
            raise ValueError("Expected a window that ends after it starts")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_SELECTION:
            raise ValueError("Expected a page of 1..10000 accounts and a nonnegative offset")
        return await self._read(window, selector, values, after, before, at, offset, limit, bool(include_battles))
