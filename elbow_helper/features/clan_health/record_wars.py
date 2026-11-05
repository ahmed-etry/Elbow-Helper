"""Regular and league wars for clan health, from the family record.

The collectors check every clan's war and league; clan health asks for no war
itself. Health keeps old clan health's view of wars: each war as last
captured, an ongoing one at its last check, in a window that includes both
ends. A league war counts its final lineup: a player swapped out before
battle day was never in the war, so never joined or missed it.

Regular means the wars the record holds as regular. Wars the record proves
friendly are left out. A count the record does not know adds nothing: it
never becomes a used or missed attack.

Health analysis runs on worker threads, so each read runs to completion here.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Iterable

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag

SECOND = 1_000_000
# Reads take at most this many accounts at once.
TAGS_PER_READ = 200
# One read covers at most this much time; the family's wars in it stay far below a read's war bound.
SPAN = 120 * 86400 * SECOND
# The family record holds no war attacks older than this.
FLOOR = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp())
PAGE_ROWS = 200
CAPTURE_TIMES = (("end_at", ("warEnded",)), ("observed_at", ("preparation", "inWar")))


async def capture_page(reads, method, *args, **kwargs):
    """Health selects ended wars by end, running wars by last check, within half-open bounds."""
    # A preparation lineup can still change and has not participated yet.
    capture_times = (CAPTURE_TIMES[0], ("observed_at", ("inWar",))) if kwargs.get("types") == ("CWL",) else CAPTURE_TIMES
    result = await getattr(reads, method)(*args, time_field=capture_times, **kwargs)
    if method == "attacks":
        positions = {}
        for war in result["lineups"]:
            if war["coverage"]["opponent"]["lineup_complete"]:
                members = sorted(war["members"], key=lambda member: (member["map_position"], member["player_tag"]))
                positions[war["war_id"], war["clan_tag"]] = {
                    member["player_tag"]: index for index, member in enumerate(members, 1)}
        for row in result["rows"]:
            places = positions.get((row["war_id"], row["clan_tag"]), {})
            if row["defender_basis"] == "lineup" and row["defender_tag"] in places:
                row["defender_position"] = places[row["defender_tag"]]
    return result


def newest_attacks(*sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Regular and league attack rows together, newest war first, each war's attacks in order."""
    rows = [row for source in sources for row in source]
    rows.sort(key=lambda row: int(row.get("attack_order") or 0))
    rows.sort(key=lambda row: (int(row.get("end_ts") or 0), str(row.get("war_id") or "")), reverse=True)
    return rows


def attack_row(row: dict[str, Any], codes: dict[str, str]) -> dict[str, Any]:
    """A record attack as clan health's attack rows show it, by clan codes keyed by tag."""
    return {
        "war_id": row["war_id"], "war_type": row["type"], "clan_code": codes.get(row["clan_tag"], ""),
        "clan_tag": row["clan_tag"], "end_ts": row["time"] // SECOND, "war_state": row["state"],
        "player_tag": row["attacker_tag"], "player_name": row["attacker_name"],
        "attack_order": row["attack_order"], "defender_tag": row["defender_tag"],
        "defender_name": row["defender_name"],
        # Positions are only known from a complete lineup; old rows wrote 0 when unknown.
        "defender_map_position": row["defender_position"] or 0,
        "defender_townhall": row["defender_townhall"] or 0,
        "stars": row["stars"], "destruction": row["destruction"],
        # None when an earlier attack on that defender may be missing.
        "fresh_attack": row["fresh"], "duration": row["duration"], "source": "family_record"}


def _window(start_ts: int, end_ts: int) -> tuple[int, int]:
    """Whole seconds, both ends included, as old clan health compared them."""
    return int(start_ts) * SECOND, int(end_ts) * SECOND + SECOND


def _spans(start_ts: int, end_ts: int) -> list[tuple[int, int]]:
    """The window in pieces that share no instant, newest first, each far below a read's war bound."""
    after, before = _window(start_ts, end_ts)
    spans = []
    while before > after:
        start = max(after, before - SPAN)
        spans.append((start, before))
        before = start
    return spans


class RegularWars:
    """War evidence of one kind for clan health, read on the calling worker thread.

    Regular wars by default; ``types=("CWL",)`` reads league wars the same way.
    """

    def __init__(self, reads, clans=CLANS, types=("REG",)):
        self.reads = reads
        self.types = tuple(types)
        # Family clan tags by clan code.
        self.tags = {code: clan.tag for code, clan in clans.items()}
        self.codes = {tag: code for code, tag in self.tags.items()}

    def _run(self, coroutine):
        return asyncio.run(coroutine)

    def _clans(self, clan_codes: Iterable[str] | None = None) -> list[str]:
        codes = self.tags if clan_codes is None else [code for code in clan_codes if code in self.tags]
        return [self.tags[code] for code in codes]

    def activity(self, *, cycle_start_ts: int, cycle_end_ts: int,
                 player_tags: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Each account's wars across the family, by account tag."""
        # Rows carry the record's tag spelling; health rows keep their own. A
        # tag that is not a valid Clash tag sat in no war.
        spelled = {}
        for tag in player_tags:
            canonical = normalize_player_tag(tag)
            if canonical is not None:
                spelled.setdefault(canonical, str(tag))
        selected = sorted(spelled)
        result = {}
        for after, before in _spans(cycle_start_ts, cycle_end_ts) if selected else ():
            for index in range(0, len(selected), TAGS_PER_READ):
                chunk = selected[index:index + TAGS_PER_READ]
                totals = self._run(capture_page(self.reads, "totals", self._clans(), after, before,
                                                player_tags=chunk, types=self.types))
                for row in totals["rows"]:
                    tag = spelled[row["player_tag"]]
                    total = result.setdefault(tag, dict.fromkeys(row, 0) | {"player_tag": row["player_tag"]})
                    for field, value in row.items():
                        if field != "player_tag":
                            total[field] += value
        return result

    def war_counts(self, *, cycle_start_ts: int, cycle_end_ts: int,
                   clan_codes: Iterable[str] | None = None) -> dict[str, int]:
        """Wars with a stored lineup, per clan code."""
        clans = self._clans(clan_codes)
        result = {}
        for after, before in _spans(cycle_start_ts, cycle_end_ts) if clans else ():
            counts = self._run(capture_page(self.reads, "counts", clans, after, before, types=self.types))
            for tag, value in counts["clans"].items():
                if value["wars_with_lineup"]:
                    code = self.codes[tag]
                    result[code] = result.get(code, 0) + value["wars_with_lineup"]
        return result

    def _attack_rows(self, player_tag: str, cycle_start_ts: int, cycle_end_ts: int, limit: int | None):
        player_tag = normalize_player_tag(player_tag)
        if player_tag is None:
            return []
        rows = []
        for after, before in _spans(cycle_start_ts, cycle_end_ts):
            offset = 0
            while offset is not None and (limit is None or len(rows) < limit):
                page = self._run(capture_page(self.reads, "attacks", self._clans(), after, before,
                                              player_tags=[player_tag], types=self.types, limit=PAGE_ROWS, offset=offset))
                rows.extend(page["rows"])
                offset = page["next_offset"]
        return rows if limit is None else rows[:limit]

    def attacks(self, *, player_tag: str, cycle_start_ts: int, cycle_end_ts: int) -> list[dict[str, Any]]:
        """The account's attacks, newest war first, as clan health's attack rows."""
        return [attack_row(row, self.codes)
                for row in self._attack_rows(player_tag, cycle_start_ts, cycle_end_ts, None)]

    def latest_attack_ts(self, *, player_tag: str, after_ts: int = FLOOR, until_ts: int | None = None) -> int | None:
        """When the account's newest attack was last captured, if after ``after_ts``."""
        end = int(until_ts if until_ts is not None else datetime.now(timezone.utc).timestamp())
        rows = self._attack_rows(player_tag, max(int(after_ts), FLOOR), end, 1)
        return rows[0]["time"] // SECOND if rows else None
