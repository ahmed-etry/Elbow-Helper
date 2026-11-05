"""Saved capital raid weekends and each member's line, with which clan-weekends have lines.

A window is [after, before) in UTC epoch microseconds over weekend start times;
``as_of`` drops weekends that began after it. A weekend's lines are the last
values saved, and a member who left the clan keeps theirs. A clan-weekend whose
payload listed no members has none, so ``has_member_lines`` is the check, not
the weekend alone. Clash sends member lists only for the latest weekends, so an
older weekend's payload may list none (``payload_listed_members`` false), unlike
a weekend where nobody attacked (listed, but empty). Never asks Clash.
"""

import json

from ..store.api_fields import read_fields
from ..store.war_projection import api_time
from .core import MAX_PAGE, RecordReads, pagination, placeholders, tags, timestamp


def clan_source(connection, clan_tag):
    """Whether the weekend history was read in full, and how the last read went."""
    row = connection.execute("SELECT * FROM family_api_state WHERE source_kind='capital_raids' AND scope_id=?",
                             (clan_tag,)).fetchone()
    if row is None:
        return {"history_complete": False, "last_attempt_at": None, "last_status": None}
    return {"history_complete": bool(json.loads(row["checkpoint_json"]).get("history_complete")),
            "last_attempt_at": row["last_attempt_at"], "last_status": row["last_status"]}


def window(connection, clans, after, before, at, offset, limit):
    end = min(before, at + 1)
    rows = connection.execute(
        "SELECT e.scope_id,e.source_at,e.final_at,e.record_scope FROM family_api_entities e "
        f"WHERE e.source_kind='capital_raids' AND e.scope_id IN ({placeholders(clans)}) "
        "AND e.source_at>=? AND e.source_at<? ORDER BY e.source_at,e.scope_id LIMIT ? OFFSET ?",
        (*clans, after, end, limit + 1, offset)).fetchall()
    more = len(rows) > limit
    weekends = []
    for row in rows[:limit]:
        fields = read_fields(connection, "capital_raids", row["record_scope"])
        lines = [dict(line) for line in connection.execute(
            "SELECT player_tag,account_id,attacks,attack_limit,bonus_attack_limit,capital_loot,observed_at "
            "FROM raid_member_lines WHERE clan_tag=? AND weekend_start=? ORDER BY player_tag",
            (row["scope_id"], row["source_at"]))]
        weekends.append({
            "clan_tag": row["scope_id"], "start_at": row["source_at"], "end_at": api_time(fields.get("/endTime")),
            "state": fields.get("/state"), "final": row["final_at"] is not None, "final_at": row["final_at"],
            "has_member_lines": bool(lines), "payload_listed_members": any(path.startswith("/members") for path in fields),
            "lines_observed_at": max((line["observed_at"] for line in lines), default=None),
            "members": lines})
    seen = {weekend["clan_tag"] for weekend in weekends}
    return {
        "after": after, "before": before, "as_of": at, "clan_tags": list(clans), "weekends": weekends,
        "offset": offset, "next_offset": offset + limit if more else None, "truncated": more,
        "coverage": {
            "weekends": len(weekends),
            "with_member_lines": sum(weekend["has_member_lines"] for weekend in weekends),
            "without_member_lines": [{"clan_tag": weekend["clan_tag"], "start_at": weekend["start_at"],
                                      "payload_listed_members": weekend["payload_listed_members"]}
                                     for weekend in weekends if not weekend["has_member_lines"]],
            "unobserved_clans": [clan for clan in clans if clan not in seen],
            "complete": bool(weekends) and not more and all(weekend["has_member_lines"] for weekend in weekends),
            "sources": {clan: clan_source(connection, clan) for clan in clans}},
        "privacy_redacted": False}


def ended(connection, after, before, selected):
    """Weekends with member lines that ended in [after, before), the copied old-only ones included.

    A weekend runs three days, so only starts up to that far before the window
    can end inside it; the end itself comes from the weekend's payload, or from
    the old file for a weekend the record never saw.
    """
    lines = "SELECT player_tag,attacks,attack_limit,bonus_attack_limit,capital_loot FROM raid_member_lines " \
            "WHERE clan_tag=? AND weekend_start=?"
    weekends = []
    for row in connection.execute(
            "SELECT e.scope_id AS clan_tag,e.source_at AS start_at,e.record_scope,o.end_at AS old_end "
            "FROM family_api_entities e LEFT JOIN old_raid_weekends o ON o.clan_tag=e.scope_id "
            "AND o.weekend_start=e.source_at WHERE e.source_kind='capital_raids' AND e.source_at>=? AND e.source_at<? "
            "UNION ALL SELECT o.clan_tag,o.weekend_start,NULL,o.end_at FROM old_raid_weekends o "
            "WHERE o.weekend_start>=? AND o.weekend_start<? AND NOT EXISTS (SELECT 1 FROM family_api_entities e "
            "WHERE e.source_kind='capital_raids' AND e.scope_id=o.clan_tag AND e.source_at=o.weekend_start) "
            "ORDER BY 2,1", (after - LONGEST, before, after - LONGEST, before)):
        end = row["old_end"] if row["record_scope"] is None else api_time(
            read_fields(connection, "capital_raids", row["record_scope"]).get("/endTime"))
        if end is None or not after <= end < before:
            continue
        members = [dict(line) for line in connection.execute(lines, (row["clan_tag"], row["start_at"]))]
        if not members:
            continue
        weekends.append({"clan_tag": row["clan_tag"], "start_at": row["start_at"], "end_at": end,
                         "origin": "record" if row["record_scope"] is not None else "legacy_health",
                         "members": [line for line in members if selected is None or line["player_tag"] in selected]})
    return {"after": after, "before": before, "weekends": weekends,
            "weekend_ends": sorted({weekend["end_at"] for weekend in weekends})}


# A capital raid weekend lasts three days; a longer window still finds every weekend ending inside it.
LONGEST = 4 * 86400 * 1_000_000


class RaidReads(RecordReads):
    """Raid weekends and member lines from the family record."""

    async def ended(self, *, after, before, player_tags=None):
        """Weekends with member lines that ended in [after, before) and those lines, optionally for some accounts.

        ``weekend_ends`` counts every weekend with lines, whoever is selected.
        """
        after, before = timestamp(after), timestamp(before)
        if before <= after:
            raise ValueError("Expected a window that ends after it starts")
        return await self._read(ended, after, before, None if player_tags is None else set(tags(player_tags)))

    async def window(self, clan_tags, *, after, before, as_of=None, offset=0, limit=MAX_PAGE):
        clans = tags(clan_tags)
        if not clans:
            raise ValueError("Expected at least one clan tag")
        after, before, at = timestamp(after), timestamp(before), timestamp(as_of)
        if before <= after:
            raise ValueError("Expected a window that ends after it starts")
        pagination(limit, offset)
        return await self._read(window, clans, after, before, at, offset, limit)
