"""League history and league groups for the CWL features, from the family record.

The collectors check every family clan's league group and wars; the CWL
features ask Clash for no league data of their own. History reads return the
shapes the roster, bonus and scoring code always used, so that code is
unchanged. A league group as it stands becomes the Clash-shaped war payloads
the boards, router, dashboards and transfer check use, with the same routing
fields. A war whose latest check failed or is overdue is marked stale, as a
failed fetch of a known war was; a war the collector has not saved is left out.

History runs on worker threads, so each read runs to completion here.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import logging
import sqlite3
from typing import Any, Iterable

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.cwl import is_cwl_window
from elbow_helper.features.wars.record import board_payload

from .helpers import coc_time_to_dt
from .record_rows import feature_week

LOGGER = logging.getLogger(__name__)
SECOND = 1_000_000
FAILURES = (OSError, sqlite3.Error, TimeoutError)
# What the bonus and scoring reads always returned for a war, a lineup row and an attack.
WAR_KEYS = ("war_id", "war_type", "clan_code", "cwl_season", "cwl_league", "cwl_round", "team_size",
            "attacks_per_member", "state", "end_ts")
ROSTER_KEYS = ("war_id", "player_tag", "player_name", "townhall", "map_position", "attacks_expected",
               "attacks_used", "roster_state")
ATTACK_KEYS = ("war_id", "player_tag", "player_name", "attack_order", "defender_tag", "defender_townhall",
               "defender_map_position", "stars", "destruction")
# A season needs this many ended wars to count as complete.
SEASON_WARS = 7
MAX_WARS = 5000
# Reads take at most this many war tags at once.
TAGS_PER_READ = 200


def _pick(row: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    return {key: row[key] for key in keys}


def _full(war: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in war.items() if key not in ("roster", "attacks", "record_war_id")}


def war_fresh(signal: dict[str, Any]) -> bool:
    """Whether a war's latest check is usable now, as the war board judges a current-war check."""
    if signal["gaps"]:
        return False
    if signal["status"] == "observed":
        return True
    failures = signal["failures"]
    return (signal["status"] in ("partial", "unavailable") and bool(failures)
            and failures[-1]["at"] == signal["last_attempt_at"] and failures[-1]["status"] == "partial_war")


def league_wars(group: dict[str, Any]) -> list[dict[str, Any]]:
    """The clan's league wars as Clash payloads with the fields the league features add to them."""
    total = int(group.get("round_count") or 0) or max((item["round"] for item in group["wars"]), default=0)
    # The season is the month the collector stored for the group.
    season = str(group.get("season_key") or "")
    wars = []
    for item in group["wars"]:
        war = board_payload(item["war"])
        state = war.get("state")
        war.update(_warTag=item["war_tag"], _round=item["round"], _state=state,
                   _start_dt=coc_time_to_dt(war.get("startTime")), _total_rounds=total,
                   _season_label=season, _season=season, _league=group.get("league"),
                   _coverage=item["war"]["coverage"],
                   # An ended war is final; a running one is stale once its checks stop.
                   _snapshot_stale=state != "warEnded" and not war_fresh(item["signal"]))
        wars.append(war)
    return wars


def group_checked(group: dict[str, Any]) -> bool:
    """Whether the latest group check is recent enough to say a league has not started.

    During the league days the collector checks every five minutes.
    """
    signal = group["signal"]
    return signal["last_outcome"] in ("ok", "not_in_war") and signal["status"] == "observed"


class CwlGroups:
    """Each family clan's league group as it stands, as league war payloads."""

    def __init__(self, reads, clans=CLANS):
        self.reads = reads
        self.tags = {code: clan.tag for code, clan in clans.items()}

    async def group(self, clan_code: str) -> dict[str, Any] | None:
        tag = self.tags.get(clan_code)
        if tag is None or self.reads is None:
            return None
        try:
            return await self.reads.group(tag)
        except FAILURES as error:
            LOGGER.warning("League group read failed for %s: %s", clan_code, error)
            return None

    async def wars(self, clan_code: str) -> list[dict[str, Any]]:
        """All of the clan's league wars (preparation, battle day and ended), as the router reads them."""
        group = await self.group(clan_code)
        return league_wars(group) if group is not None else []

    async def current_wars(self, clan_code: str) -> tuple[list[dict[str, Any]], list[str]]:
        """The league wars the dashboards show, with the warnings they always gave."""
        if clan_code not in self.tags:
            return [], [f"{clan_code}: clan tag not configured."]
        group = await self.group(clan_code)
        if group is None or group["season_id"] is None:
            return [], [f"{clan_code}: failed to fetch league group."]
        if not group["wars"] and not group["round_count"]:
            return [], [f"{clan_code}: no league wars found."]
        wars = league_wars(group)
        usable = [war for war in wars if not war["_snapshot_stale"] and (
            war["state"] != "warEnded" or (
                war["_coverage"]["own"]["lineup_complete"] and war["_coverage"]["own"]["attacks_complete"]
                and all(war.get(side, {}).get(field) is not None for side in ("clan", "opponent")
                        for field in ("stars", "destructionPercentage"))))]
        warnings = []
        failed = len(wars) - len(usable) + len(group["coverage"].get("missing_rounds", []))
        if failed == 1:
            warnings.append(f"{clan_code}: 1 CWL war couldn't be loaded.")
        elif failed:
            warnings.append(f"{clan_code}: {failed} CWL wars couldn't be loaded.")
        if not usable:
            warnings.append(f"No CWL wars were found for {clan_code}.")
        usable.sort(key=lambda war: (war.get("_round") or 0, war.get("_warTag") or ""))
        return usable, warnings

    async def spin_status(self, clan_code: str, now: datetime) -> str:
        """started, not_started or unavailable, as the transfer reminder checks a clan's league.

        One league a month is spun on days 1-11; outside them no league is
        starting, so no check is needed.
        """
        if not is_cwl_window(now):
            return "not_started"
        group = await self.group(clan_code)
        if group is None:
            return "unavailable"
        # A league that started this month stays started.
        if group["season_id"] is not None and group["season_key"] == now.astimezone(timezone.utc).strftime("%Y-%m"):
            return "started"
        # Not started is only as good as the latest check: a league may have started since.
        return "not_started" if group_checked(group) else "unavailable"


class CwlHistory:
    """Stored league history in the old shapes, read on the calling worker thread.

    ``directory`` is clan health's account directory, which names accounts the
    bot saw in family clans, former members included; it reads the same record.
    """

    def __init__(self, reads, directory=None, clans=CLANS):
        self.reads = reads
        self.directory = directory
        self.tags = {code: clan.tag for code, clan in clans.items()}
        self.codes = {tag: code for code, tag in self.tags.items()}

    def _run(self, coroutine):
        return asyncio.run(coroutine)

    def _clans(self, clan_codes: Iterable[str] | None = None) -> list[str]:
        codes = self.tags if clan_codes is None else [code for code in clan_codes if code in self.tags]
        return [self.tags[code] for code in codes]

    def _weeks(self, clan_codes=None, **selection) -> list[dict[str, Any]]:
        clans = self._clans(clan_codes)
        weeks = [feature_week(week, self.codes) for week in
                 self._run(self.reads.weeks(clans, **selection))["weeks"]] if clans else []
        return sorted(weeks, key=lambda week: (max(war["end_ts"] for war in week["wars"]),
                                              week["clan_code"], week["season"]))

    def _wars(self, clan_codes=None, **selection) -> list[dict[str, Any]]:
        return [war for week in self._weeks(clan_codes, **selection) for war in week["wars"]]

    def cache_revision(self) -> str | None:
        """The record's revision; the directory's names come from the same record."""
        return self.reads.cache_revision()

    def seasonal_cohort(self, *, clan_code, season=None):
        selected = self._run(self.reads.cohort(self.tags[clan_code], season=season))
        if selected is not None:
            selected['observed_at'] = datetime.fromtimestamp(
                selected['observed_at'] / SECOND, timezone.utc).isoformat()
        return selected

    def account_names(self) -> list[dict[str, str]]:
        """Names of accounts seen in family clans and in their war lineups, former members included."""
        names = set()
        if self.directory is not None:
            names.update((row["player_tag"], row["player_name"]) for row in self.directory.directory_names()
                         if row["player_name"] is not None)
        names.update((row["player_tag"], row["player_name"]) for row in
                     self._run(self.reads.account_names(self._clans()))["rows"])
        return [{"player_tag": tag, "player_name": name} for tag, name in sorted(names)]

    def bonus_seasons(self, clan_codes: list[str] | None = None) -> list[str]:
        codes = [str(code) for code in clan_codes or () if str(code)] or None
        return sorted({war["cwl_season"] for war in self._wars(codes, details=False)}, reverse=True)

    def bonus_wars(self, clan_code: str, season: str) -> list[dict[str, Any]]:
        wars = self._wars([clan_code], seasons=[season])
        return self._bonus_shape(wars)

    @staticmethod
    def _bonus_shape(wars: list[dict[str, Any]]) -> list[dict[str, Any]]:
        wars = sorted(wars, key=lambda war: (war["cwl_round"], war["end_ts"], war["war_id"]))
        return [{**_pick(war, WAR_KEYS),
                 "coverage": war["coverage"],
                 "roster": [_pick(row, ROSTER_KEYS) for row in war["roster"]],
                 "attacks": [_pick(row, ATTACK_KEYS) for row in war["attacks"]]} for war in wars]

    def scoring_wars(self, clan_code: str, war_ids: list[str]) -> list[dict[str, Any]]:
        """Exactly these ended wars of the clan; any other ID is refused with why."""
        if not 1 <= len(war_ids) <= 56 or any(not isinstance(key, str) or not key for key in war_ids):
            raise ValueError("Expected 1..56 war IDs")
        tag = self.tags[clan_code]
        resolved = self._run(self.reads.resolve(war_ids))["matches"]
        own_tags = [war["war_tag"] for wars in resolved.values() for war in wars
                    if war["type"] == "CWL" and war["state"] == "warEnded" and tag in war["clan_tags"]]
        own = self._wars([clan_code], war_tags=own_tags) if own_tags else []
        listed = {war["war_id"].removeprefix("CWL:") for war in own}
        errors, unknown, other, unfinished, not_league = [], set(), set(), set(), set()
        for key in war_ids:
            if not resolved[key]:
                unknown.add(key)
            for war in resolved[key]:
                shown = f"CWL:{war['war_tag']}" if war["war_tag"] else key
                if tag not in war["clan_tags"]:
                    other.add(shown)
                elif war["type"] != "CWL":
                    not_league.add(shown)
                elif war["state"] != "warEnded":
                    unfinished.add(shown)
                elif war["war_tag"] not in listed:
                    # Saved outside any league week the clan's group check found.
                    unknown.add(key)
        for label, values in (("Unknown CWL war IDs", sorted(unknown)),
                              (f"Wars from another clan than {clan_code}", sorted(other)),
                              ("Unfinished CWL wars", sorted(unfinished)), ("Not CWL wars", sorted(not_league))):
            if values:
                errors.append(label + ": " + ", ".join(values))
        if errors:
            raise ValueError("; ".join(errors) + ".")
        return self._bonus_shape(own)

    def roster_history(self, history_limit: int | None, *, season: str | None = None,
                       clan_code: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """Complete seasons (seven ended wars) per clan, newest first, at most ``history_limit`` per clan."""
        empty = {"seasons": [], "wars": [], "roster": [], "attacks": []}
        wars = self._wars([clan_code] if clan_code is not None else None,
                          seasons=[season] if season is not None else None)
        groups = defaultdict(list)
        for war in wars:
            groups[war["cwl_season"], war["clan_code"]].append(war)
        latest = {key: max(war["end_ts"] for war in group) for key, group in groups.items()
                  if len({war["war_id"] for war in group}) >= SEASON_WARS}
        by_clan = defaultdict(list)
        for key in latest:
            by_clan[key[1]].append(key)
        selected = []
        for keys in by_clan.values():
            ordered = sorted(keys, key=lambda key: (latest[key], key[0]), reverse=True)
            selected.extend(ordered[:max(1, int(history_limit))] if history_limit is not None else ordered)
        by_season = {}
        for key in selected:
            by_season[key[0]] = max(by_season.get(key[0], 0), latest[key])
        seasons = [{"key": key, "latest_end_ts": end}
                   for key, end in sorted(by_season.items(), key=lambda item: (item[1], item[0]), reverse=True)]
        if not seasons:
            return empty
        chosen = sorted((war for key in selected for war in groups[key]),
                        key=lambda war: (war["end_ts"], war["clan_code"], war["cwl_round"], war["war_id"]))
        return {
            "seasons": seasons, "wars": [_full(war) for war in chosen],
            "roster": sorted((row for war in chosen for row in war["roster"]), key=lambda row: (
                row["clan_code"], row["war_id"], row["map_position"], row["player_name"])),
            "attacks": sorted((row for war in chosen for row in war["attacks"]), key=lambda row: (
                row["clan_code"], row["war_id"], row["attack_order"], row["player_name"]))}

    def attendance(self, *, clan_codes, war_type, after_ts=None, before_ts=None, season=None, war_ids=(),
                   latest_completed=None, season_keys=(), history_limit=None, running=False):
        """League-war participation from stored lineups, in the old attendance shape.

        Ended wars count from their final lineups. With ``running``, a chosen
        season, window or set of wars also holds its wars still under way, with
        attacks so far and no misses; a window takes them once they have started.
        Recent seasons and latest wars stay completed wars.
        """
        if war_type != "CWL":
            raise ValueError("Stored war history holds league wars only; regular wars are in the family record.")
        if history_limit is not None:
            if type(history_limit) is not int or not 1 <= history_limit <= 12:
                raise ValueError("Invalid CWL history limit")
            if season or season_keys or war_ids or latest_completed or after_ts is not None or before_ts is not None:
                raise ValueError("Choose recent seasons or an explicit period, not both.")
        completed_only = not running or history_limit is not None or latest_completed is not None
        wars = self._wars(clan_codes, details=False, ended=completed_only)
        if len(wars) > MAX_WARS:
            raise ValueError("Choose fewer clans; stored war identities exceed the snapshot bound.")
        component_seasons = list(season_keys)
        if history_limit is not None:
            groups = defaultdict(list)
            for war in wars:
                groups[war["cwl_season"], war["clan_code"]].append(war)
            completed = {}
            for (key, _), group in groups.items():
                if len(group) >= SEASON_WARS:
                    completed[key] = max(completed.get(key, 0), max(war["end_ts"] for war in group))
            component_seasons = sorted(completed, key=lambda key: (completed[key], key), reverse=True)[:history_limit]
        requested = set(war_ids)
        selected = [war for war in wars if not (
            after_ts is not None and war["end_ts"] < after_ts
            or before_ts is not None and war["end_ts" if war["state"] == "warEnded" else "start_ts"] >= before_ts
            or season is not None and war["cwl_season"] != season
            or (season_keys or history_limit is not None) and war["cwl_season"] not in component_seasons
            or requested and war["war_id"] not in requested)]
        selected.sort(key=lambda war: (-war["end_ts"], war["clan_code"], war["war_id"]))
        if latest_completed is not None:
            selected = selected[:latest_completed]
        details = {}
        tags = sorted({war["war_id"].removeprefix("CWL:") for war in selected})
        for index in range(0, len(tags), TAGS_PER_READ):
            details.update(((war["war_id"], war["clan_code"]), war) for war in self._wars(
                sorted({war["clan_code"] for war in selected}), war_tags=tags[index:index + TAGS_PER_READ],
                ended=completed_only))
        rows, empty, sources, unproven, coverage = [], [], [], set(), []
        for summary in selected:
            war = details.get((summary["war_id"], summary["clan_code"]), {**summary, "roster": [], "attacks": []})
            coverage.append({**war["coverage"], "war_id": war["war_id"], "clan_code": war["clan_code"]})
            if not war["roster"]:
                empty.append(war["war_id"])
                unproven.add(war["war_id"])
            own = war["coverage"]["own"]
            causes = [cause for cause, missing in (
                ("not_final", war["state"] != "warEnded"), ("lineup_incomplete", not own["lineup_complete"]),
                ("attacks_incomplete", not own["attacks_complete"])) if missing]
            sources.append({"war_id": war["war_id"], "clan_code": war["clan_code"], "season": war["cwl_season"],
                            "aliases": [war["war_id"]], "sources": [war["source"]]})
            attacks = defaultdict(list)
            for attack in war["attacks"]:
                attacks[attack["player_tag"]].append(attack)
            for member in sorted(war["roster"], key=lambda row: row["player_tag"]):
                tag, expected = member["player_tag"], member["attacks_expected"]
                measured = attacks.get(tag, [])
                used = member["attacks_used"]
                complete = member["missed"] is not None
                if not complete:
                    unproven.add(war["war_id"])
                stars = [attack["stars"] for attack in measured if type(attack["stars"]) is int and 0 <= attack["stars"] <= 3]
                destruction = [attack["destruction"] for attack in measured
                               if type(attack["destruction"]) in (int, float) and 0 <= attack["destruction"] <= 100]
                rows.append({
                    "war_id": war["war_id"], "war_aliases": [war["war_id"]], "clan_code": war["clan_code"],
                    "war_type": "CWL", "season": war["cwl_season"], "start_ts": war["start_ts"],
                    "end_ts": war["end_ts"], "player_tag": tag, "player_name": member["player_name"],
                    "attacks_expected": expected if complete else None, "attacks_used": used,
                    "missed": member["missed"], "final_capture": complete,
                    "uncertainty_reasons": [] if complete else causes,
                    "observed_attack_performance": {
                        "attacks": len(measured), "stars": sum(stars), "stars_measured": len(stars),
                        "triples": sum(value == 3 for value in stars), "destruction_total": sum(destruction),
                        "destruction_measured": len(destruction)}})
        return {"rows": rows, "war_ids": sorted({war["war_id"] for war in selected}),
                **({"component_seasons": component_seasons,
                    "clan_seasons": [{"season": key, "clan_code": code,
                                      "wars": sum(war["clan_code"] == code and war["cwl_season"] == key
                                                  for war in selected)}
                                     for key in component_seasons for code in clan_codes]}
                   if season_keys or history_limit is not None else {}),
                "war_aliases": sources, "wars_without_rosters": sorted(empty),
                "identity_conflicts": [], "unproven_war_ids": sorted(unproven), "duplicate_identities_removed": 0,
                "coverage": {"query_complete": True, "historical_collection_complete": False,
                             "exact_ranking": not (empty or unproven), "wars": coverage,
                             "wars_not_final": sum(war["state"] != "warEnded" for war in selected),
                             "basis": "family record league wars in the selection; ended wars from final lineups, "
                                      "wars_not_final with attacks so far and no misses"}}
