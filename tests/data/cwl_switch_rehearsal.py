"""Test-only rehearsal of switching from the old CWL code to this one in the middle of a league.

Run from the repository root:
``python -m tests.data.cwl_switch_rehearsal <data folder> <work folder> <main code folder> [report file]``.
The data folder holds ``clan_health/clan_health.db``, ``war/war_cache.json`` and ``family/family.sqlite3``; the
main code folder holds the ``elbow_helper`` package of the commit before the move. Nothing calls Discord, Clash or
an LLM: a fake Clash client answers league requests from synthetic November leagues of two family clans (their
real October lineups, synthetic opponents), and a fake Discord keeps the messages the bot posts, so they outlive
a restart as they do in Discord.

One synthetic league runs from its preparation day to its last battle day, with a lineup swap in round 4's
preparation and one missed attack every round. The ticks are what the bot does: the family collector checks
every 30 s (the record's own schedule decides what is due), the router polls every 60 s, the dashboards refresh
every 20 min, the old league writer runs every 20 min and 95 s after each war ends. Around every day change
and the swap they are dense, elsewhere sparse. The router, the war boards, the thread status boards and the
dashboards are the real mixins running on real state files (router state, board messages, thread boards,
dashboard messages) in the run folder.

Each switch point runs the old code up to a restart, then either the old code again (the control: what a plain
restart does) or this code on the same state files, the same record and the same Discord messages. Their
posts, messages, router state, and (at settled checkpoints) every board, thread status, dashboard and transfer
check must agree; the history surfaces at the end are compared as the move rehearsal compares them.
"""

import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.configuration.clans import CLAN_NAMES, CLANS
from elbow_helper.infrastructure.clash.client import ClashResponse

from .cwl_move_rehearsal import CWL_CODES, DAY, GROUP_CLANS, SECOND, FakeClash, League, Side, differences, plain, prepare

FIRST_PREP = datetime(2026, 11, 3, 8, tzinfo=timezone.utc)
# Round 4's preparation starts the day before round 4's battle day; its lineup changes 12 h in.
SWAP_ROUND = 3
BOOT = FIRST_PREP - timedelta(minutes=30)
END = datetime(2026, 11, 11, 11, tzinfo=timezone.utc)
BOT_ID = 999
STICKY = {"BE1": 7001, "BEC": 7002}
# Where the bot restarts: (stop, start, which of the router's first poll and the collector's first pass comes first).
POINTS = {
    "after-round-3": (datetime(2026, 11, 7, 9, 58, tzinfo=timezone.utc), datetime(2026, 11, 7, 10, 1, tzinfo=timezone.utc),
                      "router_first"),
    "preparation-day": (datetime(2026, 11, 6, 19, 58, tzinfo=timezone.utc),
                        datetime(2026, 11, 6, 20, 1, tzinfo=timezone.utc), "router_first"),
    "battle-day": (datetime(2026, 11, 7, 19, 58, tzinfo=timezone.utc), datetime(2026, 11, 7, 20, 1, tzinfo=timezone.utc),
                   "router_first"),
    "war-ends-in-gap": (datetime(2026, 11, 7, 7, 58, 30, tzinfo=timezone.utc),
                        datetime(2026, 11, 7, 8, 1, 30, tzinfo=timezone.utc), "router_first"),
    "war-ends-in-gap-collector-first": (datetime(2026, 11, 7, 7, 58, 30, tzinfo=timezone.utc),
                                        datetime(2026, 11, 7, 8, 1, 30, tzinfo=timezone.utc), "collector_first"),
}
# A clan's helper sets the Clan Castle status in a preparation day, once before and once after the restarts.
CC_ACTIONS = ((datetime(2026, 11, 6, 12, tzinfo=timezone.utc), "BE1", "filled"),
              (datetime(2026, 11, 8, 12, tzinfo=timezone.utc), "BEC", "partial"))


def leagues_for_switch(spec):
    return [League(code, i, "V", spec["rosters"][code], FIRST_PREP, SWAP_ROUND) for i, code in enumerate(GROUP_CLANS)]


def day_changes(league):
    """The moments a war starts or ends (each also starts the next preparation day)."""
    return [league.first_prep + k * DAY for k in range(0, 9)]


# Fake Clash ---------------------------------------------------------------------------------------

class SwitchClash(FakeClash):
    """The move rehearsal's Clash, also answering regular-war checks with 'not in war'."""

    async def get(self, path, **kwargs):
        clean = path.split("?")[0]
        if clean.endswith("/currentwar"):
            return ClashResponse(200, {"state": "notInWar"}, {}, 1, 2)
        if clean.endswith("/warlog"):
            return ClashResponse(200, {"items": [], "paging": {"cursors": {}}}, {}, 1, 2)
        return await super().get(path, **kwargs)


# Fake Discord -----------------------------------------------------------------------------------------

def not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown message")


def strip(embed):
    payload = dict(embed)
    payload.pop("timestamp", None)
    return payload


class FakeMessage:
    def __init__(self, world, channel_id, message_id, content, embeds, components):
        self.world, self.id, self.content = world, message_id, content
        self.channel = SimpleNamespace(id=channel_id)
        self.author = SimpleNamespace(id=BOT_ID, bot=True)
        self._embeds, self._components = embeds, components

    @property
    def embeds(self):
        return [discord.Embed.from_dict(item) for item in self._embeds]

    @property
    def components(self):
        return [SimpleNamespace(to_dict=lambda item=item: item) for item in self._components]

    async def delete(self):
        self.world.delete(self)

    async def edit(self, **fields):
        return self.world.edit(self, fields)


class World:
    """Messages of every channel the CWL features post to, and a log of what was sent, edited and deleted."""

    def __init__(self, clock, label):
        self.clock, self.label = clock, label
        self.messages, self.next_id, self.actions = {}, 1000, []
        self.channels = {}
        self.kinds = {}

    @classmethod
    def load(cls, path, clock, label):
        world = cls(clock, label)
        if Path(path).exists():
            saved = json.loads(Path(path).read_text(encoding="utf-8"))
            world.next_id, world.actions = saved["next_id"], saved["actions"]
            world.messages = {int(mid): FakeMessage(world, item["channel"], int(mid), item["content"], item["embeds"],
                                                    item["components"]) for mid, item in saved["messages"].items()}
        return world

    def save(self, path):
        Path(path).write_text(json.dumps({
            "next_id": self.next_id, "actions": self.actions,
            "messages": {str(mid): {"channel": m.channel.id, "content": m.content, "embeds": m._embeds,
                                    "components": m._components} for mid, m in self.messages.items()}},
            sort_keys=True), encoding="utf-8")

    def log(self, op, channel_id, message, embeds):
        first = embeds[0] if embeds else {}
        self.actions.append({"t": self.clock.at.isoformat(), "seg": self.label, "op": op, "channel": channel_id,
                             "message": message, "title": first.get("title"),
                             "description": first.get("description"), "embeds": len(embeds)})

    def post(self, channel, content, embeds, view):
        self.next_id += 1
        dicts = [item.to_dict() for item in embeds]
        components = view.to_components() if view is not None else []
        message = FakeMessage(self, channel.id, self.next_id, content, dicts, components)
        self.messages[message.id] = message
        self.log("send", channel.id, message.id, dicts)
        return message

    def edit(self, message, fields):
        if message.id not in self.messages:
            raise not_found()
        if "embed" in fields and fields["embed"] is not None:
            message._embeds = [fields["embed"].to_dict()]
        elif "embeds" in fields and fields["embeds"] is not None:
            message._embeds = [item.to_dict() for item in fields["embeds"]]
        if "content" in fields:
            message.content = fields["content"]
        if "view" in fields:
            message._components = fields["view"].to_components() if fields["view"] is not None else []
        self.log("edit", message.channel.id, message.id, message._embeds)
        return True

    def delete(self, message):
        if self.messages.pop(message.id, None) is None:
            raise not_found()
        self.log("delete", message.channel.id, message.id, message._embeds)

    def channel(self, channel_id, info_ids):
        if channel_id not in self.channels:
            cls = FakeText if channel_id in info_ids else FakeThread
            self.channels[channel_id] = cls(self, channel_id)
        return self.channels[channel_id]


class ChannelMixin:
    async def send(self, content=None, *, embed=None, embeds=None, view=None, file=None, files=None, **_):
        return self.world.post(self, content, [embed] if embed is not None else list(embeds or []), view)

    async def fetch_message(self, message_id):
        message = self.world.messages.get(int(message_id))
        if message is None or message.channel.id != self.id:
            raise not_found()
        return message

    async def history(self, *, limit=None, after=None, oldest_first=False):
        found = sorted((m for m in self.world.messages.values() if m.channel.id == self.id
                        and (after is None or m.id > after.id)), key=lambda m: m.id, reverse=not oldest_first)
        for message in found[:limit]:
            yield message


class FakeText(ChannelMixin, discord.TextChannel):
    def __init__(self, world, channel_id):
        self.world, self.id, self.name = world, channel_id, f"channel-{channel_id}"


class FakeThread(ChannelMixin, discord.Thread):
    def __init__(self, world, channel_id):
        self.world, self.id, self.name = world, channel_id, f"thread-{channel_id}"
        self.archived = self.locked = False


class FakePublishing:
    def __init__(self, world):
        self.world = world

    async def edit_message(self, message, **fields):
        from elbow_helper.infrastructure.discord.publishing import PublishResult
        try:
            return PublishResult(self.world.edit(message, fields))
        except discord.NotFound as error:
            return PublishResult(False, error=error)

    async def edit_channel(self, channel, **fields):
        from elbow_helper.infrastructure.discord.publishing import PublishResult
        for key in ("archived", "locked"):
            if key in fields:
                setattr(channel, key, fields[key])
        return PublishResult(True)


# Time -----------------------------------------------------------------------------------------------

class Clock:
    def __init__(self, at):
        self.at = at


def fake_datetime(clock):
    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = cls.fromtimestamp(clock.at.timestamp(), timezone.utc)
            return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            return cls.fromtimestamp(clock.at.timestamp(), timezone.utc).replace(tzinfo=None)

    return FakeDatetime


def quick_sqlite(stack, *paths):
    """No fsync, and the databases stay open: a rehearsal on throwaway copies need not wait for the disk.

    Without another open connection every closed one would checkpoint and delete the write-ahead log.
    """
    from contextlib import contextmanager
    import sqlite3
    from elbow_helper.infrastructure.persistence import sqlite as base
    real = base.sqlite_connection

    @contextmanager
    def quick(path, **options):
        with real(path, **{**options, "synchronous": "OFF"}) as connection:
            yield connection

    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "sqlite_connection", None) is real:
            stack.enter_context(patch.object(module, "sqlite_connection", quick))
    for path in paths:
        keeper = sqlite3.connect(path)
        keeper.execute("SELECT COUNT(*) FROM sqlite_master").fetchall()
        stack.callback(keeper.close)


def patch_time(stack, clock):
    """The simulated moment for every clock the CWL features, the family record and the router read."""
    today = fake_datetime(clock)
    stack.enter_context(patch("time.time", side_effect=lambda: clock.at.timestamp()))
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith("elbow_helper"):
            continue
        if getattr(module, "datetime", None) is datetime:
            stack.enter_context(patch.object(module, "datetime", today))
        if callable(getattr(module, "now_us", None)):
            stack.enter_context(patch.object(module, "now_us", lambda: int(clock.at.timestamp()) * SECOND))


# One bot ------------------------------------------------------------------------------------------------

def build_cog(version, run, spec, world, fake):
    """The real CWL mixins that read league data, on real state files in the run folder, and a fake Discord."""
    import copy
    from elbow_helper.configuration.channels import CLAN_CWL_INFO_CHANNELS
    from elbow_helper.domain.cwl import is_cwl_window
    from elbow_helper.features.clan_health.database import ClanHealthRepository
    from elbow_helper.features.cwl.bonus.analysis import BonusAnalysisService
    from elbow_helper.features.cwl.config import CWL_THREADS, DASHBOARD_THREADS, THREAD_CLAN_CONFIGS, THREAD_STATE_FILE
    from elbow_helper.features.cwl.dashboard import CwlDashboardMixin
    from elbow_helper.features.cwl.router import CwlRouterMixin
    from elbow_helper.features.cwl.threads import CwlThreadMixin
    from elbow_helper.features.cwl.threads.emojis import EMPTY_CWL_THREAD_EMOJIS
    from elbow_helper.features.cwl.transfers import CwlTransferMixin
    from elbow_helper.features.cwl.war_board import CwlWarBoardMixin
    from elbow_helper.features.wars.emojis import EMPTY_WAR_EMOJIS

    info_ids = set(CLAN_CWL_INFO_CHANNELS.values())
    known = info_ids | set(CWL_THREADS.values()) | set(DASHBOARD_THREADS.values()) | set(STICKY.values())

    class Cog(CwlRouterMixin, CwlWarBoardMixin, CwlThreadMixin, CwlDashboardMixin, CwlTransferMixin):
        def _is_cwl_window(self):
            return is_cwl_window()

        async def _fetch_hero_sums(self, tags):
            return {}

        async def _fetch_clan_war_league_name(self, clan_tag):
            return spec["profiles"].get(clan_tag) or None

        def _render_cwl_stars_image(self, clan_name, clan_tag, period, rows, bonuses):
            self.rows[self.rendering]["stars"] = {"period": period, "rows": rows, "bonuses": bonuses}
            return discord.File(BytesIO(b""), filename="cwl_stars_summary.png")

        def _render_prep_matchup_image(self, own, opponent, subtitle, rows, status_text=None, status_color=None):
            self.rows[self.rendering]["prep"] = {"own": own, "opponent": opponent, "subtitle": subtitle, "rows": rows,
                                                 "status": status_text}
            return discord.File(BytesIO(b""), filename="cwl_prep_matchup.png")

    class Bot:
        """The application emoji provider keeps a weak reference to its client."""

    bot = Bot()
    bot.user, bot.publishing = SimpleNamespace(id=BOT_ID), FakePublishing(world)
    bot.get_channel = lambda channel_id: world.channel(channel_id, info_ids) if channel_id in known else None
    bot.fetch_channel = AsyncMock(side_effect=lambda channel_id: world.channel(channel_id, info_ids)
                                  if channel_id in known else (_ for _ in ()).throw(not_found()))
    cog = Cog()
    cog.bot, cog.clash_client, cog.rows, cog.rendering = bot, fake, {}, None
    cog.data_file = str(THREAD_STATE_FILE)
    cog.clan_configs = copy.deepcopy(THREAD_CLAN_CONFIGS)
    cog.init_thread_feature()
    cog.cwl_thread_emojis = SimpleNamespace(get=AsyncMock(return_value=EMPTY_CWL_THREAD_EMOJIS))
    cog.cwl_war_emojis = SimpleNamespace(get=AsyncMock(return_value=EMPTY_WAR_EMOJIS))
    cog.account_links = SimpleNamespace(get_clan_badge_url=lambda code: None)
    cog.dashboard_state = cog._load_dashboard_state()
    cog._hero_sum_cache, cog._clan_league_cache = {}, {}
    cog._manual_dashboard_refresh_locks, cog._manual_dashboard_refresh_last_ts = {}, {}
    cog._dashboard_refresh_warning_state = {}
    cog.state = cog._load_state()
    cog.cwl_board_registry = cog.state.setdefault("cwl_board_messages", {})
    cog._cwl_board_locks = {}
    repository = ClanHealthRepository(Path(run) / "clan_health.db")
    if version == "old":
        cog._leaguegroup_cache, cog._war_cache = {}, {}
        cog.bonus_analysis = BonusAnalysisService(fake, repository)
        cog.repository = repository
    else:
        from elbow_helper.data.reads import CwlReads
        from elbow_helper.data.store import FamilyStore
        from elbow_helper.features.cwl.record import CwlGroups, CwlHistory
        cog.store = FamilyStore(Path(run) / "family.sqlite3")
        reads = CwlReads(cog.store)
        cog.cwl_groups = CwlGroups(reads)
        cog.cwl_history = CwlHistory(reads, repository)
        cog.bonus_analysis = BonusAnalysisService(fake, cog.cwl_history, cog.cwl_groups)
    return cog


def seed_state(run):
    """The thread boards two clans' helpers registered before the league."""
    path = Path(run) / "data" / "cwl" / "cwl_threads.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        threads = {str(STICKY[code]): {"clan_name": CLAN_NAMES[code], "cc_status": {}, "cc_statuses": {}}
                   for code in GROUP_CLANS}
        path.write_text(json.dumps({"threads": threads, "settings": {"auto_reset_last_ym": ""}}), encoding="utf-8")


# Ticks --------------------------------------------------------------------------------------------------

def make_ticks(start, end, boot_order, changes, swap):
    """(moment, kind) in order. Dense around every day change, the swap and the start; sparse elsewhere."""
    windows = [(moment - timedelta(minutes=3), moment + timedelta(minutes=9)) for moment in [*changes, swap]]
    windows.append((start, start + timedelta(minutes=10)))

    def dense(moment):
        return any(low <= moment <= high for low, high in windows)

    # The collector's own check is every 30 s; its first pass is just after or just before the router's first poll.
    offset = 5 if boot_order == "router_first" else 0
    ticks, k = [], 0
    while start + timedelta(seconds=60 * k) <= end:
        moment = start + timedelta(seconds=60 * k)
        if k % 30 == 0 or dense(moment):
            ticks.append((moment, "poll"))
        k += 1
    k = 0
    # The collector's schedule check is every 30 s all the time: a source not checked within a minute of its due
    # time is stale to the readers, so a sparse collector would make every reader see stale wars.
    while start + timedelta(seconds=offset + 30 * k) <= end:
        ticks.append((start + timedelta(seconds=offset + 30 * k), "collect"))
        k += 1
    # The old league writer's end-of-war capture, 95 s after each war ends.
    ticks.extend((moment + timedelta(seconds=95), "ingest") for moment in changes[2:] if start <= moment + timedelta(seconds=95) <= end)
    # Checkpoints are at fixed moments, whichever code runs: 12 minutes past the half hour.
    moment = start.replace(minute=12, second=0, microsecond=0)
    while moment <= end:
        if moment > start:
            ticks.append((moment, "checkpoint"))
        moment += timedelta(minutes=30)
    ticks.extend((moment, "cc") for moment, _, _ in CC_ACTIONS if start <= moment <= end)
    order = {"collect": 0, "ingest": 1, "poll": 2, "cc": 3, "checkpoint": 9}
    return sorted(ticks, key=lambda tick: (tick[0], order[tick[1]]))


# A segment: one run of one code version --------------------------------------------------------------------

async def run_segment(version, run, work, start, end, label, boot_order):
    os.chdir(run)
    spec = json.loads((Path(work) / "spec.json").read_text(encoding="utf-8"))
    leagues = leagues_for_switch(spec)
    changes = day_changes(leagues[0])
    swap = leagues[0].times(SWAP_ROUND)[0] + timedelta(hours=12)
    clock = Clock(start)
    world = World.load(Path(run) / "discord.json", clock, label)
    fake = SwitchClash(leagues, spec["profiles"])
    fake.at = start
    seed_state(run)
    from elbow_helper.data.collectors.service import FamilyCollectors
    from elbow_helper.data.store import FamilyStore
    log = {"version": version, "label": label, "start": start.isoformat(), "end": end.isoformat(), "checkpoints": [],
           "spin": [], "rows": []}
    with ExitStack() as stack:
        cog = build_cog(version, run, spec, world, fake)
        collector_store = cog.store if version == "new" else FamilyStore(Path(run) / "family.sqlite3")
        await collector_store.initialize()
        bot = SimpleNamespace(boot_complete=asyncio.Event(), guild_id=0, get_guild=lambda guild_id: None)
        collectors = FamilyCollectors(bot, collector_store, fake, CLANS.values())
        if version == "old":
            from elbow_helper.features.clan_health.api import ClanHealthCollector
            writer = ClanHealthCollector(fake, cog.repository, None)
            stack.enter_context(patch("elbow_helper.features.clan_health.api.is_cwl_window", return_value=True))
            stack.enter_context(patch.object(writer, "_schedule_cwl_war_end_capture"))
        patch_time(stack, clock)
        quick_sqlite(stack, Path(run) / "family.sqlite3", Path(run) / "clan_health.db")
        last_dash = last_ingest = None
        collected = False
        previous_spin = None

        async def spin(at):
            started, unavailable = await cog._cwl_spin_statuses(set(CWL_CODES), at)
            return {"started": sorted(started), "unavailable": sorted(unavailable)}

        async def dashboards():
            for code in (*GROUP_CLANS, "BEH"):
                cog.rendering = code
                cog.rows.setdefault(code, {})
                await cog._refresh_dashboard_with_retry(code, "loop")

        for at, kind in make_ticks(start, end, boot_order, changes, swap):
            clock.at = fake.at = at
            if kind == "checkpoint":
                (Path(run) / f"progress-{label}.txt").write_text(at.isoformat(), encoding="utf-8")
            if kind == "collect":
                await collectors.collect_wars()
                collected = True
            elif kind == "ingest":
                if version == "old":
                    await writer.ingest_family_cwl_activity()
            elif kind == "poll":
                await cog._poll_once()
                # The dashboards' first refresh after a start waits for the collectors' first war pass.
                if collected and (last_dash is None or at - last_dash >= timedelta(minutes=20)):
                    last_dash = at
                    await dashboards()
                if version == "old" and (last_ingest is None or at - last_ingest >= timedelta(minutes=20)):
                    last_ingest = at
                    await writer.ingest_family_cwl_activity()
                current = await spin(at)
                if current != previous_spin:
                    previous_spin = current
                    log["spin"].append({"at": at.isoformat(), **current})
            elif kind == "cc":
                for moment, code, status in CC_ACTIONS:
                    if moment == at:
                        thread_id = STICKY[code]
                        sticky = cog.data["threads"][str(thread_id)].get("sticky_message_id")
                        log["cc_" + code] = await cog.set_cwl_cc_status(code, status, SimpleNamespace(id=42), thread_id,
                                                                        sticky, access_checked=True)
            else:
                log["checkpoints"].append(await checkpoint(cog, world, at, spin))
        log["rows"] = cog.rows
    world.save(Path(run) / "discord.json")
    log["actions"] = [item for item in world.actions if item["seg"] == label]
    (Path(run) / f"segment-{label}.json").write_text(json.dumps(log, sort_keys=True, default=str), encoding="utf-8")


async def checkpoint(cog, world, at, spin):
    """Everything a member can see of the CWL features at a settled moment, and the router's memory."""

    def message(channel_id, message_id):
        found = world.messages.get(message_id) if message_id else None
        return None if found is None or found.channel.id != channel_id else {
            "id": message_id, "embeds": [strip(item) for item in found._embeds], "components": found._components}

    from elbow_helper.features.cwl.config import DASHBOARD_THREADS
    state = {"at": at.isoformat(), "spin": await spin(at), "clans": {}}
    for code in GROUP_CLANS:
        registry = cog.cwl_board_registry.get(code)
        thread_key = str(STICKY[code])
        thread_data = cog.data["threads"].get(thread_key, {})
        dashboard = {}
        for kind in ("prep_message_ids", "stars_message_ids"):
            dashboard[kind] = message(DASHBOARD_THREADS[code], cog.dashboard_state[kind].get(code))
        state["clans"][code] = {
            "board": message(registry["channel"], registry["message"]) if registry else None,
            "sticky": message(STICKY[code], thread_data.get("sticky_message_id")),
            "thread_data": {key: thread_data.get(key) for key in ("cc_statuses", "active_prep", "cc_status")},
            "dashboard": dashboard, "rendered": cog.rows.get(code)}
    state["router"] = {key: cog.state.get(key) for key in ("rosters", "last_war_tag", "missed_posted", "missed_pending")}
    # The baseline lists a lineup in set order, which differs from one process to the next.
    state["router"]["rosters"] = {code: {tag: sorted(members) for tag, members in wars.items()}
                                  for code, wars in (cog.state.get("rosters") or {}).items()}
    state["router"]["last_poll_ts"] = cog.state.get("last_poll_ts")
    return plain(state)


async def run_final(version, run, work, at, output):
    """The history surfaces after the league, computed the way the move rehearsal computes them."""
    spec = json.loads((Path(work) / "spec.json").read_text(encoding="utf-8"))
    clock = Clock(at)
    with ExitStack() as stack:
        side = Side(version, work, spec, folder=run)
        patch_time(stack, clock)
        side.fake.leagues, side.fake.at = leagues_for_switch(spec), at
        result = {"history": await asyncio.to_thread(side.history_surfaces, at), "agent": await side.agent_tools(),
                  "live": await side.live(at)}
    Path(output).write_text(json.dumps(result, sort_keys=True, default=str), encoding="utf-8")


# The rehearsal --------------------------------------------------------------------------------------------

def run_python(root, cwd, *arguments):
    # The agent's test context imports its helpers from tests/, as discovery from tests/ does.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(cwd), str(root), str(root / "tests")])}
    result = subprocess.run([sys.executable, "-m", "tests.data.cwl_switch_rehearsal", *map(str, arguments)], cwd=cwd,
                            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"{arguments[:3]} failed:\n{result.stderr[-6000:]}")


def copy_run(source, target):
    if Path(target).exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def one_point(name, work, main_code, root):
    stop, restart, order = POINTS[name]
    first = Path(work) / name / "first"
    # A point always starts from the copies; leftovers of an earlier run would be state it did not make.
    shutil.rmtree(Path(work) / name, ignore_errors=True)
    first.mkdir(parents=True)
    shutil.copyfile(Path(work) / "old" / "clan_health.db", first / "clan_health.db")
    shutil.copyfile(Path(work) / "new" / "family.sqlite3", first / "family.sqlite3")
    run_python(root, main_code, "segment", "old", first, work, BOOT.isoformat(), stop.isoformat(), "first", "router_first")
    for kind, version, cwd in (("control", "old", main_code), ("switch", "new", root)):
        folder = Path(work) / name / kind
        copy_run(first, folder)
        run_python(root, cwd, "segment", version, folder, work, restart.isoformat(), END.isoformat(), kind, order)
        run_python(root, cwd, "final", version, folder, work, END.isoformat(), folder / "final.json")


def read_log(folder, label):
    return json.loads((Path(folder) / f"segment-{label}.json").read_text(encoding="utf-8"))


def post_key(action):
    return (action["channel"], action["title"], action["description"])


def compare_point(name, work):
    """Everything the switch run did differently from the control, in groups."""
    stop, restart, _ = POINTS[name]
    base = Path(work) / name
    found = {"point": name}
    first, control, switch = (read_log(base / folder, label) for folder, label in (
        ("first", "first"), ("control", "control"), ("switch", "switch")))
    sends = {}
    for kind, log in (("control", control), ("switch", switch)):
        sends[kind] = [action for action in [*first["actions"], *log["actions"]] if action["op"] == "send"]
    count = lambda actions: Counter(post_key(action) for action in actions)
    found["sends"] = {kind: len(items) for kind, items in sends.items()}
    found["send_differences"] = [
        {"post": key, "control": count(sends["control"])[key], "switch": count(sends["switch"])[key]}
        for key in sorted(set(count(sends["control"])) | set(count(sends["switch"])), key=str)
        if count(sends["control"])[key] != count(sends["switch"])[key]]
    found["repeated_posts"] = {kind: [key for key, n in count(items).items() if n > 1] for kind, items in sends.items()}
    # What each version posts after the restart, and when.
    found["after_restart"] = {kind: [(a["t"], a["op"], a["channel"], a["title"]) for a in log["actions"]
                                     if a["op"] != "edit"] for kind, log in (("control", control), ("switch", switch))}
    other = {"edit": Counter(), "delete": Counter()}
    for kind, log in (("control", control), ("switch", switch)):
        for op in other:
            other[op][kind] = sum(action["op"] == op for action in [*first["actions"], *log["actions"]])
    found["edits"], found["deletes"] = dict(other["edit"]), dict(other["delete"])
    # Settled checkpoints after the restart, between the two runs.
    mismatches = []
    pairs = list(zip(control["checkpoints"], switch["checkpoints"]))
    for left, right in pairs:
        if left["at"] != right["at"]:
            mismatches.append({"at": left["at"], "problem": "checkpoint moments differ"})
            continue
        for item in differences(left, right):
            mismatches.append({"at": left["at"], **item})
    found["checkpoints"] = {"compared": len(pairs), "mismatching": len({item["at"] for item in mismatches})}
    found["mismatches"] = mismatches[:60]
    found["spin_changes"] = {"control": control["spin"], "switch": switch["spin"]}
    found["cc"] = {key: (control.get(key), switch.get(key)) for key in ("cc_BE1", "cc_BEC")}
    final = [json.loads((base / kind / "final.json").read_text(encoding="utf-8")) for kind in ("control", "switch")]
    found["final_differences"] = differences(*final)
    return found


def rehearse(data, work, main_code, points=None):
    work, main_code = Path(work), Path(main_code).resolve()
    # The copies and the spec are made once; every point only reads them.
    if not (work / "spec.json").exists():
        prepare(data, work)
    root = Path(__file__).resolve().parents[2]
    names = list(points or POINTS)
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        list(pool.map(lambda name: one_point(name, work, main_code, root), names))
    return {name: compare_point(name, work) for name in names}


if __name__ == "__main__":
    command = sys.argv[1]
    if command == "segment":
        version, run, work, start, end, label, order = sys.argv[2:9]
        asyncio.run(run_segment(version, run, work, datetime.fromisoformat(start), datetime.fromisoformat(end), label,
                                order))
    elif command == "final":
        version, run, work, at, output = sys.argv[2:7]
        asyncio.run(run_final(version, run, work, datetime.fromisoformat(at), output))
    else:
        report = json.dumps(rehearse(*sys.argv[1:4], points=sys.argv[5].split(",") if len(sys.argv) > 5 else None),
                            indent=1, sort_keys=True, default=str)
        if len(sys.argv) > 4 and sys.argv[4] != "-":
            Path(sys.argv[4]).write_text(report, encoding="utf-8")
        else:
            print(report)
