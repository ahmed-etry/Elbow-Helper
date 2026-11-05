"""Clan health's activity, rosters, stays and account directory, from the family record.

Health works its verdicts out when a report is asked for, from these reads;
nothing here asks Clash or stores a verdict. Windows are whole seconds with
both ends included, as old clan health compared them. Health analysis runs on
worker threads, so each read runs to completion here.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Iterable

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag

SECOND = 1_000_000
# Reads take at most this many accounts at once.
TAGS_PER_READ = 200


def _seconds(value):
    return None if value is None else int(value) // SECOND


class HealthRecord:
    """Record reads for clan health, run to completion on the calling worker thread."""

    def __init__(self, activity, raids, stays, clans=CLANS):
        self.activity_reads = activity
        self.raid_reads = raids
        self.stay_reads = stays
        self.tags = {code: clan.tag for code, clan in clans.items()}
        self.codes = {tag: code for code, tag in self.tags.items()}

    def _run(self, coroutine):
        return asyncio.run(coroutine)

    @staticmethod
    def _spelled(player_tags: Iterable[str]) -> dict[str, str]:
        """Record spelling -> the caller's spelling; a tag that is not a Clash tag has no record."""
        spelled = {}
        for tag in player_tags:
            canonical = normalize_player_tag(tag)
            if canonical is not None:
                spelled.setdefault(canonical, str(tag))
        return spelled

    def _chunks(self, player_tags):
        spelled = self._spelled(player_tags)
        selected = sorted(spelled)
        for index in range(0, len(selected), TAGS_PER_READ):
            yield spelled, selected[index:index + TAGS_PER_READ]

    def progress(self, player_tags: Iterable[str], *, at_ts: int, until_ts: int | None = None) -> dict[str, dict]:
        """Each account's progress at ``at_ts``; failing that, its first values up to ``until_ts``."""
        result = {}
        for spelled, chunk in self._chunks(player_tags):
            page = self._run(self.activity_reads.progress(
                chunk, as_of=int(at_ts) * SECOND + SECOND - 1,
                until=None if until_ts is None else int(until_ts) * SECOND + SECOND - 1))
            for row in page["rows"]:
                if row["known"]:
                    result[spelled[row["player_tag"]]] = row
        return result

    def donations(self, player_tags: Iterable[str], *, cycle_start_ts: int, cycle_end_ts: int) -> dict[str, dict]:
        """Donations and received after the start, up to and including the end."""
        result = {}
        for spelled, chunk in self._chunks(player_tags):
            page = self._run(self.activity_reads.donations(
                chunk, after=int(cycle_start_ts) * SECOND + SECOND - 1, before=int(cycle_end_ts) * SECOND + SECOND))
            for row in page["rows"]:
                result[spelled[row["player_tag"]]] = row
        return result

    def raid_lines(self, *, cycle_start_ts: int, cycle_end_ts: int,
                   player_tags: Iterable[str] | None = None) -> tuple[list[dict], int]:
        """Member lines of the weekends that ended in the window, and how many weekends ended in it."""
        spelled = None if player_tags is None else self._spelled(player_tags)
        page = self._run(self.raid_reads.ended(after=int(cycle_start_ts) * SECOND,
                                               before=int(cycle_end_ts) * SECOND + SECOND))
        lines = []
        for weekend in page["weekends"]:
            for line in weekend["members"]:
                if spelled is not None and line["player_tag"] not in spelled:
                    continue
                lines.append({**line, "player_tag": spelled[line["player_tag"]] if spelled else line["player_tag"],
                              "clan_tag": weekend["clan_tag"], "clan_code": self.codes.get(weekend["clan_tag"], ""),
                              "end_ts": weekend["end_at"] // SECOND,
                              "weekend_id": f"{weekend['clan_tag']}:{weekend['start_at']}",
                              "attacks": int(line["attacks"] or 0), "loot": int(line["capital_loot"] or 0)})
        return lines, len(page["weekend_ends"])

    def raid_activity(self, *, cycle_start_ts: int, cycle_end_ts: int,
                      player_tags: Iterable[str]) -> tuple[dict[str, dict], int]:
        """Per account: weekends with attacks, attacks and capital gold; and the weekends that ended in the window."""
        lines, weekends = self.raid_lines(cycle_start_ts=cycle_start_ts, cycle_end_ts=cycle_end_ts,
                                          player_tags=player_tags)
        totals = defaultdict(lambda: {"attacks": 0, "loot": 0, "ends": set()})
        for line in lines:
            total = totals[line["player_tag"]]
            total["attacks"] += line["attacks"]
            total["loot"] += line["loot"]
            if line["attacks"] > 0:
                total["ends"].add(line["end_ts"])
        return {tag: {"attacks": value["attacks"], "loot": value["loot"],
                      "raid_weekends_participated": len(value["ends"])} for tag, value in totals.items()}, weekends

    def roster(self, clan_codes: Iterable[str], *, at_ts: int) -> tuple[int | None, dict[str, list[dict]]]:
        """Each clan's members at the moment, by clan code, and when that roster was observed (seconds)."""
        codes = [code for code in clan_codes if code in self.tags]
        page = self._run(self.stay_reads.roster([self.tags[code] for code in codes],
                                                at=int(at_ts) * SECOND + SECOND - 1))
        observed = [clan["observed_at"] for clan in page["clans"] if clan["observed_at"] is not None]
        members = {self.codes[clan["clan_tag"]]: clan["members"] for clan in page["clans"]}
        return (_seconds(max(observed)) if observed else None), members

    def stays(self, player_tag: str, *, cycle_start_ts: int, cycle_end_ts: int) -> list[dict]:
        """The account's clan stays that overlap the window, as seen inside it, in seconds, oldest first."""
        tag = normalize_player_tag(player_tag)
        if tag is None:
            return []
        start, end = int(cycle_start_ts) * SECOND, int(cycle_end_ts) * SECOND + SECOND
        page = self._run(self.stay_reads.stays([tag], after=start, before=end, as_of=end - 1))
        merged = []
        for stay in page["rows"][0]["stays"]:
            previous = merged[-1] if merged else None
            if (previous is not None and previous["clan_tag"] == stay["clan_tag"]
                    and previous["to_at"] is not None and stay["from_at"] <= previous["to_at"]):
                # A copied old stay the record's own membership carries on: one stay.
                previous.update(to_at=stay["to_at"], last_seen_at=stay["last_seen_at"], basis=stay["basis"])
            else:
                merged.append(dict(stay))
        return [{"clan_code": stay["clan_code"] or self.codes.get(stay["clan_tag"]) or "-",
                 "clan_tag": stay["clan_tag"], "start_ts": _seconds(max(stay["first_seen_at"], start)),
                 "end_ts": _seconds(min(stay["last_seen_at"], end - 1)), "ongoing": stay["to_at"] is None,
                 "basis": stay["basis"]} for stay in merged]

    # The player directory, for autocomplete, attack plans and CWL.

    def search_players(self, current: str, limit: int = 25, *, offset: int = 0,
                       clan_code: str | None = None) -> list[dict[str, Any]]:
        page = self._run(self.stay_reads.lookup(str(current or "")[:100], clan_code=clan_code,
                                                limit=max(1, min(26, int(limit))), offset=offset))
        return [self._directory_row(row) for row in page["rows"]]

    def directory_players(self, player_tags: Iterable[str]) -> dict[str, dict[str, Any]]:
        result = {}
        for spelled, chunk in self._chunks(player_tags):
            for row in self._run(self.stay_reads.accounts(chunk))["rows"]:
                result[spelled[row["player_tag"]]] = {**self._directory_row(row), "player_tag": spelled[row["player_tag"]]}
        return result

    def directory_names(self) -> list[dict[str, str]]:
        """Each account's latest name in a family clan, former members included."""
        return sorted(self._run(self.stay_reads.names())["rows"], key=lambda row: (row["player_tag"], row["player_name"]))

    def _directory_row(self, row):
        return {"player_tag": row["player_tag"], "player_name": row["player_name"] or row["player_tag"],
                "clan_code": row["clan_code"] or self.codes.get(row["clan_tag"]) or "",
                "townhall": int(row["townhall"] or 0), "last_seen_ts": _seconds(row["last_seen_at"])}
