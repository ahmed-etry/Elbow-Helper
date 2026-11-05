"""Clan stays and family movements from the record and the copied old health spans, and account lookup.

A record stay starts at the first roster that listed the account and ends at
the first that did not; its last sighting is the last roster that did. A copied
stay is first to last hourly capture of the old clan health file (``basis``
``observed_hourly``) and ends where the record's own membership begins. A
roster at a moment is the record's open stays then, with the old file's stays
at its last capture at or before that moment. Never asks Clash.
"""

import json

from .core import MAX_PAGE, RecordReads, pagination, placeholders, tags as checked_tags, timestamp
from .membership import stays as record_stays

# Search results per page, as the old player directory returned them.
LOOKUP_PAGE = 26


def family_codes(connection):
    return {row[0]: row[1] for row in connection.execute("SELECT tag,family_code FROM clans WHERE is_family=1")}


def account_stays(connection, tag, at, codes):
    """Every stay of the account that began at or before ``at``, oldest first."""
    result = []
    account = connection.execute("SELECT id FROM accounts WHERE player_tag=?", (tag,)).fetchone()
    if account is None:
        return result
    for row in connection.execute("SELECT clan_tag,from_at,to_at,last_observed_at FROM old_clan_stays "
                                  "WHERE account_id=? AND from_at<=? ORDER BY from_at", (account[0], at)):
        result.append({"clan_tag": row[0], "clan_code": codes.get(row[0]), "from_at": row[1],
                       "to_at": row[2] if row[2] <= at else None, "first_seen_at": row[1],
                       "last_seen_at": min(row[3], at), "basis": "observed_hourly"})
    for stay in record_stays(connection, tag, at):
        last = stay["end_previous_good_at"] if stay["to_at"] is not None else stay["last_confirmed_at"]
        result.append({"clan_tag": stay["clan_tag"], "clan_code": stay["clan_code"] or codes.get(stay["clan_tag"]),
                       "from_at": stay["from_at"], "to_at": stay["to_at"], "first_seen_at": stay["from_at"],
                       "last_seen_at": max(stay["from_at"], last if last is not None else stay["from_at"]),
                       "basis": "record"})
    result.sort(key=lambda stay: stay["from_at"])
    return result


def stays(connection, selected, after, before, at):
    codes = family_codes(connection)
    rows = []
    for tag in selected:
        found = [stay for stay in account_stays(connection, tag, min(at, before - 1), codes)
                 if stay["to_at"] is None or stay["to_at"] > after or stay["last_seen_at"] >= after]
        rows.append({"player_tag": tag, "stays": found})
    return {"after": after, "before": before, "as_of": at, "rows": rows}


def movements(connection, after, before, limit):
    """Every account's stays that overlap [after, before), with its name at each stay's start; bounded."""
    codes = family_codes(connection)
    accounts = connection.execute(
        "SELECT DISTINCT a.id,a.player_tag FROM accounts a WHERE EXISTS (SELECT 1 FROM clan_membership m "
        "JOIN clans c ON c.id=m.clan_id WHERE m.account_id=a.id AND c.is_family=1 AND m.from_at<? "
        "AND (m.to_at IS NULL OR m.to_at>?)) OR EXISTS (SELECT 1 FROM old_clan_stays s WHERE s.account_id=a.id "
        "AND s.from_at<? AND s.to_at>=?) ORDER BY a.player_tag", (before, after, before, after)).fetchall()
    rows, count = [], 0
    for account, tag in accounts:
        found = [stay for stay in account_stays(connection, tag, before - 1, codes)
                 if stay["last_seen_at"] >= after or stay["to_at"] is None]
        for stay in found:
            stay["player_name"] = name_at(connection, account, stay["from_at"])
        count += len(found)
        if count > limit:
            return {"after": after, "before": before, "rows": rows, "truncated": True}
        rows.append({"player_tag": tag, "stays": found})
    return {"after": after, "before": before, "rows": rows, "truncated": False}


def name_at(connection, account, at):
    row = connection.execute("SELECT name FROM account_aliases WHERE account_id=? AND from_at<=? "
                             "ORDER BY from_at DESC LIMIT 1", (account, at)).fetchone()
    if row:
        return row[0]
    row = connection.execute("SELECT name FROM account_aliases WHERE account_id=? ORDER BY from_at LIMIT 1",
                             (account,)).fetchone()
    return row[0] if row else connection.execute("SELECT current_name FROM accounts WHERE id=?",
                                                 (account,)).fetchone()[0]


def last_capture(connection, at):
    """The old file's last hourly capture at or before ``at``, as its copied counter samples date it."""
    return connection.execute(
        "SELECT MAX(c.sampled_at) FROM player_counters c JOIN collection_runs r ON r.id=c.source_run_id "
        "WHERE r.collector='profile_import' AND c.sampled_at<=?", (at,)).fetchone()[0]


def roster(connection, selected, at):
    """Each clan's members at ``at``, and when that roster was observed."""
    codes = family_codes(connection)
    clans = {tag: {"clan_tag": tag, "clan_code": codes.get(tag), "members": [], "observed_at": None}
             for tag in selected}
    for row in connection.execute(
            "SELECT c.tag,a.id,a.player_tag FROM clan_membership m JOIN clans c ON c.id=m.clan_id "
            f"JOIN accounts a ON a.id=m.account_id WHERE c.tag IN ({placeholders(selected)}) AND m.from_at<=? "
            "AND (m.to_at IS NULL OR m.to_at>?) ORDER BY a.player_tag", (*selected, at, at)):
        clans[row[0]]["members"].append({"player_tag": row[2], "player_name": name_at(connection, row[1], at),
                                         "basis": "record"})
    for tag, clan in clans.items():
        if clan["members"]:
            clan["observed_at"] = connection.execute(
                "SELECT MAX(fetched_at) FROM collection_items WHERE source_kind='clan_roster' AND scope_id=? "
                "AND status IN ('ok','empty') AND fetched_at<=?", (tag, at)).fetchone()[0]
    captured = last_capture(connection, at)
    if captured is not None:
        listed = {member["player_tag"] for clan in clans.values() for member in clan["members"]}
        for row in connection.execute(
                "SELECT s.clan_tag,a.id,a.player_tag FROM old_clan_stays s JOIN accounts a ON a.id=s.account_id "
                f"WHERE s.clan_tag IN ({placeholders(selected)}) AND s.from_at<=? AND s.to_at>=? ORDER BY a.player_tag",
                (*selected, captured, captured)):
            if row[2] in listed:
                continue
            clan = clans[row[0]]
            clan["members"].append({"player_tag": row[2], "player_name": name_at(connection, row[1], at),
                                    "basis": "observed_hourly"})
            clan["observed_at"] = max(clan["observed_at"] or 0, captured)
    for clan in clans.values():
        clan["members"].sort(key=lambda member: member["player_tag"])
    return {"as_of": at, "clans": [clans[tag] for tag in selected]}


def directory(connection, at):
    """Each account ever seen in a family clan: name, last family clan, town hall and last sighting."""
    codes = family_codes(connection)
    seen = {}

    def note(account, tag, clan, last):
        entry = seen.setdefault(account, {"player_tag": tag, "clan_tag": clan, "last_seen_at": last})
        if last >= entry["last_seen_at"]:
            entry.update(clan_tag=clan, last_seen_at=last)

    for row in connection.execute("SELECT s.account_id,a.player_tag,s.clan_tag,s.last_observed_at FROM old_clan_stays s "
                                  "JOIN accounts a ON a.id=s.account_id WHERE s.from_at<=?", (at,)):
        note(row[0], row[1], row[2], min(row[3], at))
    for row in connection.execute(
            "SELECT m.account_id,a.player_tag,c.tag,m.from_at,m.to_at,m.last_observed_at,m.end_previous_good_at "
            "FROM clan_membership m JOIN accounts a ON a.id=m.account_id JOIN clans c ON c.id=m.clan_id "
            "WHERE c.is_family=1 AND m.from_at<=?", (at,)):
        closed = row[4] is not None and row[4] <= at
        last = (row[6] if closed and row[6] is not None else row[5]) or row[3]
        note(row[0], row[1], row[2], max(row[3], min(last, at)))
    for account, entry in seen.items():
        townhall = connection.execute("SELECT value_json FROM player_observations WHERE account_id=? "
                                      "AND field_key='townHallLevel' AND observed_at<=? ORDER BY observed_at DESC LIMIT 1",
                                      (account, at)).fetchone()
        entry.update(player_name=name_at(connection, account, at), clan_code=codes.get(entry["clan_tag"]),
                     townhall=json.loads(townhall[0]) if townhall else None)
    return list(seen.values())


def lookup(connection, query, clan_code, at, limit, offset):
    """Old health's directory search: tag, name and clan code prefixes, most recently seen first."""
    rows = [row for row in directory(connection, at) if clan_code is None or row["clan_code"] == clan_code]
    needle = query.strip().casefold()
    tag_needle = needle.removeprefix("#")

    def keys(row):
        return (str(row["player_name"] or "").casefold(), row["player_tag"].removeprefix("#").casefold(),
                str(row["clan_code"] or "").casefold())

    def rank(row):
        name, tag, _ = keys(row)
        return (0 if tag == tag_needle else 1 if name == needle else 2 if tag.startswith(tag_needle)
                else 3 if name.startswith(needle) else 4)

    if needle:
        rows = [row for row in rows if keys(row)[0].startswith(needle) or keys(row)[1].startswith(tag_needle)
                or keys(row)[2].startswith(needle)]
    rows.sort(key=lambda row: (rank(row) if needle else 0, -row["last_seen_at"], keys(row)[0], row["player_tag"]))
    return {"rows": rows[offset:offset + limit], "as_of": at}


def accounts(connection, selected, at):
    wanted = set(selected)
    return {"as_of": at, "rows": [row for row in directory(connection, at) if row["player_tag"] in wanted]}


def names(connection, at):
    return {"as_of": at, "rows": [{"player_tag": row["player_tag"], "player_name": row["player_name"]}
                                  for row in directory(connection, at) if row["player_name"] is not None]}


class StayReads(RecordReads):
    """Clan stays, rosters and the family account directory from the family record."""

    async def stays(self, player_tags, *, after, before, as_of=None):
        """Each account's stays that overlap [after, before), record and copied old ones, oldest first."""
        after, before, at = timestamp(after), timestamp(before), timestamp(as_of)
        if before <= after:
            raise ValueError("Expected a window that ends after it starts")
        return await self._read(stays, checked_tags(player_tags), after, before, at)

    async def movements(self, *, after, before, limit=100_000):
        """Every family account's stays that overlap [after, before); ``truncated`` past ``limit`` stays."""
        after, before = timestamp(after), timestamp(before)
        if before <= after or type(limit) is not int or limit < 1:
            raise ValueError("Expected a window that ends after it starts and a positive bound")
        return await self._read(movements, after, before, limit)

    async def roster(self, clan_tags, *, at):
        clans = checked_tags(clan_tags)
        if not clans:
            raise ValueError("Expected at least one clan tag")
        return await self._read(roster, clans, timestamp(at))

    async def lookup(self, query, *, clan_code=None, as_of=None, limit=LOOKUP_PAGE, offset=0):
        if not isinstance(query, str) or len(query) > 100:
            raise ValueError("Expected a query of at most 100 characters")
        pagination(limit, offset)
        return await self._read(lookup, query, clan_code, timestamp(as_of), min(limit, MAX_PAGE), offset)

    async def accounts(self, player_tags, *, as_of=None):
        selected = checked_tags(player_tags)
        return await self._read(accounts, selected, timestamp(as_of))

    async def names(self, *, as_of=None):
        return await self._read(names, timestamp(as_of))
