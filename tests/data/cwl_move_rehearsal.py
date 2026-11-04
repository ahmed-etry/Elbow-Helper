"""Test-only rehearsal of the CWL move: the code before it against this code, on every CWL surface.

Run from the repository root:
``python -m tests.data.cwl_move_rehearsal <data folder> <work folder> <main code folder> [report file]``.
The data folder holds ``clan_health/clan_health.db``, ``war/war_cache.json`` and ``family/family.sqlite3``;
the main code folder holds the ``elbow_helper`` package of the commit before the move. Nothing calls
Discord, Clash or an LLM: a fake Clash client answers league requests from synthetic leagues of two
family clans (their real October lineups, synthetic opponents), with no league name in the group, as
Clash sends it.

Each code version runs in its own process on its own copies and feeds the same Clash answers through
its own writer: the old code through clan health's league ingest into its store, this code through
the family collector into the family record (both startup copies already run). Then both compute
every CWL surface the same way: the history reads (bonus review and scores, roster history, attendance,
performance and scoring, health numbers, player attacks, names), the live reads (league wars, boards'
rendered embeds, thread status, dashboards, transfer check) and the router's posts.

Part 1 is the running November week (round 1 ended, round 2 in battle day, round 3 in preparation,
with a lineup swap in round 3's preparation between two checks). Part 2 is a whole 7-round December
season, checked four times a round, with a swap in round 2's preparation and one missed attack a round.
"""

import asyncio
from collections import Counter
from contextlib import ExitStack, closing
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

from elbow_helper.configuration.clans import CLANS
from elbow_helper.infrastructure.clash.client import ClashResponse

CHARS = "0289PYLQGRJCUV"
SECOND = 1_000_000
DAY = timedelta(days=1)
GROUP_CLANS = ("BE1", "BEC")
CWL_CODES = tuple(code for code, clan in CLANS.items() if clan.cwl_role_id is not None)


def clash_time(moment):
    return moment.strftime("%Y%m%dT%H%M%S.000Z")


# Synthetic leagues ------------------------------------------------------------------------------

class League:
    """One family clan's group of 8 clans and 7 rounds, its state a function of time."""

    def __init__(self, code, index, part, roster, first_prep, swap_round):
        self.code, self.tag = code, CLANS[code].tag
        g = "PL"[index]
        self.opponents = [(f"#9C{part}{g}{CHARS[i]}", f"Rival {i + 1}",
                           [(f"#9Q{part}{g}{CHARS[i]}{CHARS[j // 14]}{CHARS[j % 14]}", f"Rival player {j + 1}", 15)
                            for j in range(15)]) for i in range(7)]
        self.roster = [tuple(player) for player in roster[:15]]
        self.spare = (f"#9Y{part}{g}0", "Spare player", 14)
        self.first_prep, self.swap_round = first_prep, swap_round
        self.rounds = [[f"#9{part}{g}{CHARS[r]}{CHARS[w]}" for w in range(4)] for r in range(7)]
        self.season = (first_prep + DAY).strftime("%Y-%m")

    def times(self, r):
        prep = self.first_prep + r * DAY
        return prep, prep + DAY, prep + 2 * DAY

    def state(self, r, at):
        prep, start, end = self.times(r)
        return None if at < prep else "preparation" if at < start else "inWar" if at < end else "warEnded"

    def lineup(self, r, at):
        """Before the swap in its round's preparation, the spare stands in for the last player."""
        players = list(self.roster)
        if r == self.swap_round and at < self.times(r)[0] + timedelta(hours=12):
            players[14] = self.spare
        return players

    def pairings(self, r, at):
        family = (self.tag, f"Family {self.code}", self.lineup(r, at))
        others = [o for i, o in enumerate(self.opponents) if i != r]
        return [(family, self.opponents[r])] + [(others[i], others[i + 1]) for i in range(0, 6, 2)]

    def group(self, at):
        if at < self.first_prep:
            return None
        last_end = self.times(6)[2]
        state = "ended" if at >= last_end else "inWar" if at >= self.first_prep + DAY else "preparation"
        clans = [(self.tag, f"Family {self.code}", self.roster + [self.spare]), *self.opponents]
        return {"state": state, "season": self.season,
                "clans": [{"tag": tag, "name": name, "clanLevel": 10, "badgeUrls": {"small": "https://example.test/b"},
                           "members": [{"tag": p, "name": n, "townHallLevel": th} for p, n, th in players]}
                          for tag, name, players in clans],
                "rounds": [{"warTags": self.rounds[r] if self.state(r, at) else ["#0"] * 4} for r in range(7)]}

    def war(self, tag, at):
        for r in range(7):
            if tag in self.rounds[r] and self.state(r, at):
                return self.payload(r, self.pairings(r, at)[self.rounds[r].index(tag)], at)
        return None

    def payload(self, r, pair, at):
        state = self.state(r, at)
        prep, start, end = self.times(r)
        attacking = (15 if state == "warEnded" else
                     min(15, int(15 * (at - start).total_seconds() / (end - start).total_seconds())) if state == "inWar"
                     else 0)
        attacks, order = ([], []), 1
        for i in range(attacking):
            for side in (0, 1):
                # The family's last player misses every round.
                if side == 0 and pair[0][0] == self.tag and i == 14:
                    continue
                stars = (3, 2, 1)[(i + r) % 3] if side == 0 else 2
                attacks[side].append((i, {"attackerTag": pair[side][2][i][0], "defenderTag": pair[1 - side][2][i][0],
                                          "stars": stars, "destructionPercentage": (100.0, 80.0, 55.0)[(i + r) % 3]
                                          if side == 0 else 75.0, "order": order, "duration": 120}))
                order += 1
        sides = []
        for side, (tag, name, players) in enumerate(pair):
            mine, theirs = attacks[side], attacks[1 - side]
            sides.append({"tag": tag, "name": name, "clanLevel": 10, "badgeUrls": {"small": "https://example.test/b"},
                          "attacks": len(mine), "stars": sum(a["stars"] for _, a in mine),
                          "destructionPercentage": sum(a["destructionPercentage"] for _, a in mine) / 15,
                          "members": [{"tag": p, "name": n, "townhallLevel": th, "mapPosition": i + 1,
                                       "opponentAttacks": sum(1 for j, _ in theirs if j == i),
                                       "attacks": [a for j, a in mine if j == i]}
                                      for i, (p, n, th) in enumerate(players)]})
        return {"state": state, "teamSize": 15, "attacksPerMember": 1, "preparationStartTime": clash_time(prep),
                "startTime": clash_time(start), "endTime": clash_time(end), "clan": sides[0], "opponent": sides[1]}


class FakeClash:
    """Answers league groups, league wars and clan profiles from the leagues at ``at``; anything else fails."""

    configured = True

    def __init__(self, leagues, profiles):
        self.leagues, self.profiles, self.at = leagues, profiles, None

    async def get(self, path, **kwargs):
        path = unquote(path).split("?")[0]
        if path.startswith("/clanwarleagues/wars/"):
            tag = path.rsplit("/", 1)[1]
            for league in self.leagues:
                payload = league.war(tag, self.at)
                if payload is not None:
                    return ClashResponse(200, payload, {}, 1, 2)
            return ClashResponse(404, {"reason": "notFound"}, {}, 1, 2)
        parts = path.split("/")
        if path.endswith("/currentwar/leaguegroup"):
            league = next((league for league in self.leagues if league.tag == parts[2]), None)
            group = league.group(self.at) if league else None
            return ClashResponse(200, group, {}, 1, 2) if group else ClashResponse(404, {"reason": "notFound"}, {}, 1, 2)
        if len(parts) == 3 and parts[1] == "clans" and parts[2] in self.profiles:
            return ClashResponse(200, {"tag": parts[2], "warLeague": {"name": self.profiles[parts[2]]}}, {}, 1, 2)
        return ClashResponse(503, {"reason": "inMaintenance"}, {}, 1, 2)


def leagues_for(spec, part):
    if part == "november":
        # Round 1 ended two hours ago; round 3's lineup changes halfway through its preparation.
        now = datetime.fromisoformat(spec["now"])
        first = now - timedelta(hours=2) - 2 * DAY
        return [League(code, i, "V", spec["rosters"][code], first, 2) for i, code in enumerate(GROUP_CLANS)]
    first = datetime(2026, 12, 1, 8, tzinfo=timezone.utc)
    return [League(code, i, "U", spec["rosters"][code], first, 1) for i, code in enumerate(GROUP_CLANS)]


def december_steps(league):
    steps = set()
    for r in range(7):
        prep, start, end = league.times(r)
        steps.update((prep + timedelta(hours=1), prep + timedelta(hours=13), end - timedelta(hours=1),
                      end + timedelta(seconds=120)))
    return sorted(steps)


# One code version ------------------------------------------------------------------------------

def plain(value):
    """JSON-ready, floats rounded, dataclasses and datetimes flattened."""
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [plain(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True)) if isinstance(value, (set, frozenset)) \
            else items
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# Each answer's own identity and timing, which no two reads share.
VOLATILE = {"report_id", "next_cursor", "observed_at", "source_versions", "as_of", "generated_at", "created_at",
            "read_at"}


def settled(value):
    if isinstance(value, dict):
        return {key: settled(item) for key, item in value.items() if key not in VOLATILE}
    if isinstance(value, list):
        return [settled(item) for item in value]
    return plain(value)


def embed(item):
    payload = item.to_dict()
    payload.pop("timestamp", None)
    for key in ("thumbnail", "image"):
        if isinstance(payload.get(key), dict):
            payload[key] = {k: v for k, v in payload[key].items() if k == "url"}
    return payload


class Clock:
    """The record's clock, set to each step of the simulation; the old code keeps real time."""

    MODULES = ("elbow_helper.data.store", "elbow_helper.data.reads.core", "elbow_helper.data.collectors.wars",
               "elbow_helper.data.collectors.service")

    def __init__(self):
        self.at = None

    def patches(self, stack):
        for module in self.MODULES:
            try:
                __import__(module)
            except ImportError:
                continue
            if hasattr(sys.modules[module], "now_us"):
                stack.enter_context(patch(f"{module}.now_us", side_effect=lambda: int(self.at.timestamp()) * SECOND))


def bonus_config():
    expected = {f"{att}:{dfn}": round(min(3.0, max(0.5, 2.2 + 0.25 * (att - dfn))), 2)
                for att in range(1, 19) for dfn in range(1, 19)}
    profile = {"matchup_expected": expected, "max_downhit": 2, "max_uphit": 8, "downhit_penalty_per_level": 0.15,
               "uphit_bonus_per_level": 0.10, "downhit_severe_after": 0, "downhit_severe_base": 0.2,
               "downhit_severe_multiplier": 2.0}
    return {"revision": 1, "clans": {code: profile for code in CLANS}, "clan_meta": {}}


class Side:
    """Every CWL surface of one code version, computed the same way for both."""

    def __init__(self, version, work, spec, folder=None):
        from elbow_helper.features.clan_health.database import ClanHealthRepository
        from elbow_helper.features.cwl.queries import CwlQueries
        from elbow_helper.features.cwl.bonus.analysis import BonusAnalysisService
        self.version, self.work, self.spec = version, Path(work), spec
        # The folder holding this version's stores; the switch rehearsal keeps both versions' stores in one.
        folder = Path(folder) if folder is not None else self.work / version
        self.fake = FakeClash([], spec["profiles"])
        self.repository = ClanHealthRepository(folder / "clan_health.db")
        config = SimpleNamespace(load=lambda: (bonus_config(), []), cache_revision=lambda: "1")
        if version == "old":
            from elbow_helper.features.clan_health.api import ClanHealthCollector
            from elbow_helper.features.cwl.router import CwlRouterMixin
            self.collector = ClanHealthCollector(self.fake, self.repository, None)
            history = self.repository
            self.bonus = BonusAnalysisService(self.fake, self.repository)
            fetcher = CwlRouterMixin()
            fetcher.clash_client, fetcher._leaguegroup_cache, fetcher._war_cache = self.fake, {}, {}
            self.fetcher = fetcher
            self.league_wars = fetcher._get_league_wars
            self.groups = None
        else:
            from elbow_helper.data.reads import CwlReads, WarReads
            from elbow_helper.data.store import FamilyStore
            from elbow_helper.features.clan_health.record_wars import RegularWars
            from elbow_helper.features.cwl.record import CwlGroups, CwlHistory
            self.store = FamilyStore(folder / "family.sqlite3")
            reads = CwlReads(self.store)
            history = CwlHistory(reads, self.repository)
            self.groups = CwlGroups(reads)
            self.bonus = BonusAnalysisService(self.fake, history, self.groups)
            self.league_wars = self.groups.wars
            self.league = RegularWars(WarReads(self.store), types=("CWL",))
        self.history = history
        self.queries = CwlQueries(history, lambda: {}, bonus_analysis=self.bonus, bonus_config=config)
        self.router = self.make_router()

    # Writers ---------------------------------------------------------------------------------------

    async def collect(self):
        if self.version == "old":
            # Steps are hours apart; the old code keeps a league group for five minutes of real time.
            self.fetcher._leaguegroup_cache.clear()
            with patch("elbow_helper.features.clan_health.api.is_cwl_window", return_value=True), \
                    patch.object(self.collector, "_schedule_cwl_war_end_capture"):
                wars, rows, warnings = await self.collector.ingest_family_cwl_activity()
            return {"wars": wars, "warnings": len(warnings)}
        from elbow_helper.data.collectors.service import FamilyCollectors
        bot = SimpleNamespace(boot_complete=asyncio.Event(), guild_id=0, get_guild=lambda guild_id: None)
        report = await FamilyCollectors(bot, self.store, self.fake, CLANS.values()).collect_wars()
        return {key: report[key] for key in ("status", "succeeded", "empty", "failed")}

    # Live surfaces ---------------------------------------------------------------------------------

    def make_router(self):
        from elbow_helper.features.cwl.router import CwlRouterMixin
        side = self

        class Router(CwlRouterMixin):
            def __init__(self):
                self.state = {"rosters": {}, "roster_names": {}, "last_war_tag": {}, "missed_posted": {},
                              "missed_pending": {}, "name_cache": {}, "last_poll_ts": 0, "cwl_board_messages": {}}
                self.posts = []

            async def _get_league_wars(self, clan_key):
                return await side.league_wars(clan_key)

            async def _save_state(self):
                return None

            async def sync_registered_cwl_thread(self, clan_key, wars):
                return None

            async def _sync_cwl_channel(self, clan_key, wars):
                return None

            async def _log_rotation_api(self, clan, added, removed, *, round_number=None):
                self.posts.append({"kind": "rotation", "clan": clan, "round": round_number,
                                   "added": len(added), "removed": len(removed),
                                   "names": [*added, "|", *removed]})

            async def _log_missed_api(self, clan, war_tag, users, *, round_number=None, end_ts=None,
                                      check_existing=False):
                self.posts.append({"kind": "missed", "clan": clan, "war_tag": war_tag, "round": round_number,
                                   "end_ts": end_ts, "users": list(users)})
                return True

        return Router()

    async def poll(self):
        before = len(self.router.posts)
        await self.router._poll_once()
        return self.router.posts[before:]

    async def dashboards(self, code):
        from elbow_helper.features.cwl.dashboard import CwlDashboardMixin
        side, captured = self, {}

        class Dashboard(CwlDashboardMixin):
            bonus_analysis = side.bonus

            def _is_cwl_window(self):
                return True

            async def _fetch_clan_war_league_name(self, clan_tag):
                return side.spec["profiles"].get(clan_tag) or None

            async def _fetch_hero_sums(self, tags):
                return {}

            def _render_cwl_stars_image(self, clan_name, clan_tag, period, rows, bonuses):
                captured["stars"] = {"period": period, "rows": rows, "bonuses": bonuses}
                return None

            def _render_prep_matchup_image(self, own, opponent, subtitle, rows, status_text=None, status_color=None):
                captured["prep"] = {"own": own, "opponent": opponent, "subtitle": subtitle, "rows": rows,
                                    "status": status_text}
                return None

        board = Dashboard()
        stars, _ = await board._build_dashboard_stars_payload(code)
        prep, _ = await board._build_prep_preview_payload(code)
        return {"stars_embed": embed(stars), "prep_embed": embed(prep), **captured}

    async def live(self, at):
        from elbow_helper.features.cwl.threads.snapshots import build_cwl_thread_snapshot
        from elbow_helper.features.cwl.war_board import (
            cwl_board_snapshot_is_complete, orient_cwl_war, select_cwl_board_wars)
        from elbow_helper.features.wars.emojis import EMPTY_WAR_EMOJIS
        from elbow_helper.features.wars.rendering import build_war_board_embed
        result = {}
        for code in (*GROUP_CLANS, "BEH"):
            tag = CLANS[code].tag
            wars = await self.league_wars(code)
            selected = select_cwl_board_wars(wars) if cwl_board_snapshot_is_complete(wars) else []
            result[code] = {
                "wars": [{key: war.get(key) for key in ("_warTag", "_round", "_state", "_total_rounds", "_season",
                                                        "_snapshot_stale")} for war in wars],
                "board_complete": cwl_board_snapshot_is_complete(wars),
                "board": [embed(build_war_board_embed(orient_cwl_war(war, tag), EMPTY_WAR_EMOJIS)) for war in selected],
                "thread": build_cwl_thread_snapshot(wars, tag, now=at),
                "dashboard": await self.dashboards(code),
            }
        result["spin"] = await self.spin(at)
        return plain(result)

    async def spin(self, at):
        from elbow_helper.features.cwl.transfers import CwlTransferMixin
        workflow = CwlTransferMixin()
        workflow.clash_client = self.fake
        workflow._get_league_wars = self.league_wars
        workflow.cwl_groups = self.groups
        started, unavailable = await workflow._cwl_spin_statuses(set(CWL_CODES), at)
        return {"started": sorted(started), "unavailable": sorted(unavailable)}

    # The agent's CWL tools, called as the planner calls them ---------------------------------------

    async def agent_tools(self):
        from dataclasses import replace
        from elbow_helper.agent.families import war_reads
        from agent.engine.test_agent_plan_flow import _context
        links = SimpleNamespace(get_links_by_tags=lambda tags: {}, get_links_for_members=lambda ids: {},
                                get_all_links=lambda: {})
        context = replace(_context(), cwl_queries=self.queries, account_links=links)
        tools = {tool.definition.name: tool for tool in war_reads.TOOLS}
        calls = [("read_cwl_results", {"metric": "performance", "history_limit": 3}),
                 ("read_cwl_results", {"metric": "attendance", "clan_codes": list(GROUP_CLANS)}),
                 ("read_cwl_results", {"metric": "ass", "clan_codes": list(GROUP_CLANS)}),
                 ("read_cwl_results", {"metric": "bonus", "clan_codes": list(GROUP_CLANS)}),
                 ("read_cwl_results", {"metric": "mega_ass", "clan_codes": list(GROUP_CLANS), "history_limit": 3}),
                 ("read_war_attendance", {"war_type": "CWL", "latest_completed": 10}),
                 ("read_war_attendance", {"war_type": "CWL", "clan_codes": list(GROUP_CLANS), "history_limit": 2})]
        result = {}
        with patch("elbow_helper.agent.families.core.require_evidence_access", AsyncMock()):
            for index, (name, arguments) in enumerate(calls):
                answer = await tools[name].handler(context, arguments)
                result[f"{index} {name} {arguments.get('metric', arguments.get('war_type'))}"] = settled(answer)
        return result

    # History surfaces ------------------------------------------------------------------------------

    def health(self, at):
        tags = set(self.spec["tags"])
        window = {"cycle_start_ts": int((at - timedelta(weeks=8)).timestamp()), "cycle_end_ts": int(at.timestamp())}
        if self.version == "old":
            rows = self.repository.cwl_war_activity(**window, player_tags=tags)
            activity = {tag: (row["cwl_events_joined"], row["cwl_attacks_expected"], row["cwl_attacks_used"],
                              row["stars"], row["destruction"], row["attack_count"]) for tag, row in rows.items()}
            counts = self.repository.cwl_war_counts(**window)
            attacks = {tag: [(row["end_ts"], row["clan_code"], row["defender_tag"], row["defender_map_position"],
                              row["defender_townhall"], row["stars"], row["destruction"], bool(row["fresh_attack"]))
                             for row in self.repository.player_cwl_attacks(
                                 player_tag=tag, cycle_start=at - timedelta(weeks=8), cycle_end=at, limit=0)]
                       for tag in self.spec["sample"]}
        else:
            rows = self.league.activity(**window, player_tags=tags)
            activity = {tag: (row["wars"], row["attacks_expected"], row["attacks_used"], row["stars"],
                              row["destruction"], row["attacks_measured"]) for tag, row in rows.items()}
            counts = self.league.war_counts(**window)
            attacks = {tag: [(row["end_ts"], row["clan_code"], row["defender_tag"], row["defender_map_position"],
                              row["defender_townhall"], row["stars"], row["destruction"], row["fresh_attack"])
                             for row in self.league.attacks(player_tag=tag, **window)]
                       for tag in self.spec["sample"]}
        return {"activity": activity, "counts": counts, "attacks": attacks}

    def history_surfaces(self, at):
        config = bonus_config()
        out = {"seasons": {code: self.bonus.seasons([code]) for code in CWL_CODES}}
        review = {}
        for code in CWL_CODES:
            for season in out["seasons"][code][:3]:
                summary, ineligible, raw, warnings, errors = self.bonus.analyze_clan(code, season, config)
                review[f"{code} {season}"] = {
                    "summary": [(r["player_tag"], r["rank"], r["attack_count"], r["avg_adjusted_delta"],
                                 r["total_actual"], r["missed_attacks"]) for r in summary],
                    "ineligible": [(r["player_tag"], r["missed_attacks"], r["expected_attacks"], r["used_attacks"],
                                    r["reason"]) for r in ineligible],
                    "raw": [(r["round"], r["war_tag"], r["player_tag"], r["defender_tag"], r["attacker_th"],
                             r["defender_th"], r["stars"], r["destruction"], r["adjusted_delta"]) for r in raw],
                    "warnings": warnings, "errors": errors}
        out["bonus_review"] = review
        scoring = {}
        for code in GROUP_CLANS:
            for season in out["seasons"][code][:2]:
                ids = [war["war_id"] for war in self.history.bonus_wars(code, season)]
                if not ids:
                    continue
                ass = self.queries.ass_wars(clan_code=code, war_ids=ids)
                bonus = self.queries.bonus_wars(clan_code=code, war_ids=ids)
                scoring[f"{code} {season}"] = {
                    "ass": {"rounds": ass.resolved_rounds, "league": ass.league, "profile": ass.profile_key,
                            "rows": [(r.player_tag, r.wars, r.attacks, r.stars, r.ass_score, r.rank,
                                      r.average_target_position, r.average_defensive_position) for r in ass.rows],
                            "attack_sample": len(ass.attack_sample)},
                    "bonus": {"summaries": [(r.player_tag, r.rank, r.attack_count, r.total_adjusted_delta)
                                            for r in bonus.summaries],
                              "ineligible": [(r.player_tag, r.missed_attacks) for r in bonus.ineligible],
                              "attacks": len(bonus.attacks)}}
        out["scoring"] = scoring
        dataset = self.history.roster_history(3)
        out["roster_history"] = {
            "seasons": dataset["seasons"],
            "wars": sorted((w["war_id"], w["clan_code"], w["cwl_season"], w["cwl_round"], w["cwl_league"], w["team_size"],
                            w["end_ts"]) for w in dataset["wars"]),
            "roster": sorted((r["war_id"], r["clan_code"], r["player_tag"], r["map_position"], r["townhall"],
                              r["attacks_expected"], r["attacks_used"]) for r in dataset["roster"]),
            "attacks": sorted((a["war_id"], a["clan_code"], a["player_tag"], a["attack_order"], a["defender_tag"],
                               a["defender_map_position"], a["defender_townhall"], a["stars"], a["destruction"],
                               int(bool(a["fresh_attack"]))) for a in dataset["attacks"])}
        performance = self.queries.performance(history_limit=3)
        out["performance"] = {
            "seasons": performance.seasons,
            "clan_seasons": [(s.season, s.clan_code, s.league, s.wars, s.accounts, s.attacks, s.attacks_expected,
                              s.complete) for s in performance.clan_seasons],
            "rows": [(r.season, r.clan_code, r.player_tag, r.wars, r.attacks, r.attacks_expected, r.stars, r.score,
                      r.rank, r.multi_season_score) for r in performance.rows]}
        attendance = {}
        for name, selection in (("recent_seasons", {"history_limit": 3}), ("latest_wars", {"latest_completed": 30}),
                                ("current_season", {"season": out["seasons"]["BE1"][0]})):
            result = self.queries.attendance(clan_codes=list(CWL_CODES), war_type="CWL", **selection)
            attendance[name] = {
                "war_ids": result["war_ids"], "without_rosters": result["wars_without_rosters"],
                "component_seasons": result.get("component_seasons"),
                "rows": sorted((r["war_id"], r["clan_code"], r["season"], r["player_tag"], r["attacks_expected"],
                                r["attacks_used"], r["missed"], r["observed_attack_performance"]["stars"])
                               for r in result["rows"])}
        out["attendance"] = attendance
        out["account_names"] = sorted({(r["player_tag"], r["player_name"]) for r in self.history.account_names()})
        out["health"] = self.health(at)
        return plain(out)


async def run_side(version, work):
    work = Path(work)
    spec = json.loads((work / "spec.json").read_text(encoding="utf-8"))
    clock = Clock()
    clock.at = datetime.fromisoformat(spec["now"])
    output = {}
    with ExitStack() as stack:
        clock.patches(stack)
        side = Side(version, work, spec)

        async def step(at, leagues):
            clock.at = side.fake.at = at
            side.fake.leagues = leagues
            return {"at": at.isoformat(), "collected": await side.collect(), "posts": await side.poll(),
                    "live": await side.live(at)}

        # Part 1: the running November week, a swap in round 3's preparation between two checks.
        november = leagues_for(spec, "november")
        now = datetime.fromisoformat(spec["now"])
        swap = november[0].times(2)[0] + timedelta(hours=12)
        output["november"] = [await step(swap - timedelta(minutes=30), november),
                              await step(max(now, swap + timedelta(minutes=30)), november)]
        clock.at = side.fake.at
        output["november_history"] = await asyncio.to_thread(side.history_surfaces, side.fake.at)
        output["november_agent"] = await side.agent_tools()
        # Part 2: a whole December season, checked four times a round.
        december = leagues_for(spec, "december")
        output["december"] = [await step(at, december) for at in december_steps(december[0])]
        output["december_history"] = await asyncio.to_thread(side.history_surfaces, side.fake.at)
        output["december_agent"] = await side.agent_tools()
    (work / f"{version}.json").write_text(json.dumps(output, sort_keys=True, default=str), encoding="utf-8")


# The rehearsal ----------------------------------------------------------------------------------

def prepare(data, work):
    """Copies for each version, the family record with both startup copies run, and the shared spec."""
    from elbow_helper.data.store import now_us
    from .war_import_rehearsal import collectors, settle
    data, work = Path(data), Path(work)
    for version in ("old", "new"):
        (work / version).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(data / "clan_health" / "clan_health.db", work / version / "clan_health.db")
    cache = work / "war_cache.json"
    shutil.copyfile(data / "war" / "war_cache.json", cache)
    family = work / "new" / "family.sqlite3"
    shutil.copyfile(data / "family" / "family.sqlite3", family)

    async def copy():
        store = await settle(family)
        await collectors(store, work / "new" / "clan_health.db", cache)._copy_old_wars()
        return await store.call(lambda c: dict(c.execute("SELECT topic,checks_json FROM imports").fetchall()))
    imports = asyncio.run(copy())
    health = work / "old" / "clan_health.db"
    with closing(sqlite3.connect(f"file:{health.as_posix()}?mode=ro", uri=True)) as old:
        rosters = {}
        for code in GROUP_CLANS:
            war_id = old.execute("SELECT war_id FROM wars WHERE war_type='CWL' AND clan_code=? ORDER BY end_ts DESC "
                                 "LIMIT 1", (code,)).fetchone()[0]
            rosters[code] = [tuple(row) for row in old.execute(
                "SELECT player_tag,player_name,townhall FROM war_roster_members WHERE war_id=? AND clan_code=? "
                "ORDER BY map_position", (war_id, code)).fetchall()[:15]]
        tags = sorted({row[0] for row in old.execute("SELECT player_tag FROM war_roster_members WHERE war_id LIKE 'CWL:%'")})
    from elbow_helper.data.store.api_fields import read_fields
    with closing(sqlite3.connect(f"file:{family.as_posix()}?mode=ro", uri=True)) as record:
        record.row_factory = sqlite3.Row
        profiles = {}
        for clan in CLANS.values():
            league = read_fields(record, "clan_profile", clan.tag).get("/warLeague/name")
            if isinstance(league, str):
                profiles[clan.tag] = league
    spare = [f"#9Y{part}{g}0" for part in "VU" for g in "PL"]
    sample = [rosters[code][i][0] for code in GROUP_CLANS for i in (0, 7, 14)] + spare[:2]
    now = datetime.fromtimestamp(now_us() // SECOND, timezone.utc)
    spec = {"now": now.isoformat(), "rosters": rosters, "profiles": profiles,
            "tags": sorted(set(tags) | {p[0] for r in rosters.values() for p in r} | set(spare)), "sample": sample}
    (work / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    return {"copies": {topic: json.loads(checks).get("inserted") for topic, checks in imports.items()
                       if topic.startswith("wars")}, "profiles_found": len(profiles)}


def differences(old, new, path=""):
    """Leaf paths where the two outputs differ, with both values."""
    if isinstance(old, dict) and isinstance(new, dict):
        found = []
        for key in sorted(set(old) | set(new)):
            found.extend(differences(old.get(key, "<missing>"), new.get(key, "<missing>"), f"{path}/{key}"))
        return found
    if isinstance(old, list) and isinstance(new, list) and old and all(isinstance(item, dict) for item in old + new) \
            and len(old) == len(new):
        found = []
        for index, (a, b) in enumerate(zip(old, new)):
            found.extend(differences(a, b, f"{path}[{index}]"))
        return found
    return [] if old == new else [{"path": path, "old": old, "new": new}]


def rehearse(data, work, main_code):
    work, main_code = Path(work), Path(main_code).resolve()
    report = {"prepare": prepare(data, work)}
    root = Path(__file__).resolve().parents[2]
    for version, cwd in (("old", main_code), ("new", root)):
        # The agent's test context imports its helpers from tests/, as discovery from tests/ does.
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(cwd), str(root), str(root / "tests")])}
        result = subprocess.run([sys.executable, "-m", "tests.data.cwl_move_rehearsal", "side", version, str(work)],
                                cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise RuntimeError(f"{version} side failed:\n{result.stderr[-6000:]}")
    old = json.loads((work / "old.json").read_text(encoding="utf-8"))
    new = json.loads((work / "new.json").read_text(encoding="utf-8"))
    # Each writer reports its own pass; that report is not a surface.
    for side in (old, new):
        for part in ("november", "december"):
            for item in side[part]:
                item.pop("collected")
    found = differences(old, new)
    (work / "differences.json").write_text(json.dumps(found, indent=1, default=str), encoding="utf-8")
    report["differences"] = len(found)
    report["by_surface"] = dict(sorted(Counter("/".join(item["path"].split("/")[:3]) for item in found).items()))
    report["steps"] = {"november": len(old["november"]), "december": len(old["december"])}
    report["posts"] = {version: Counter(post["kind"] for part in ("november", "december") for step in side[part]
                                        for post in step["posts"]) for version, side in (("old", old), ("new", new))}
    return report


if __name__ == "__main__":
    if sys.argv[1] == "side":
        asyncio.run(run_side(sys.argv[2], sys.argv[3]))
    else:
        result = json.dumps(rehearse(*sys.argv[1:4]), indent=1, sort_keys=True, default=str)
        if len(sys.argv) > 4:
            Path(sys.argv[4]).write_text(result, encoding="utf-8")
        else:
            print(result)
