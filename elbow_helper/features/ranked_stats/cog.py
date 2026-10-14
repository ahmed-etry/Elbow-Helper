"""Ranked snapshots and rolling battle logs through the shared Clash client."""

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from time import monotonic

from discord.ext import commands, tasks

from elbow_helper.core.background import start_resilient_loop
from elbow_helper.domain.player_tags import encode_clash_tag, normalize_player_tag
from .fields import project_player
from .queries import RankedQueries
from .store import RankedStore
from .battles import INITIAL_POLL_SECONDS, MAX_POLL_SECONDS, MIN_POLL_SECONDS, battle_time, parse_log


LOGGER = logging.getLogger(__name__)
PROFILE_INTERVAL_SECONDS = 3600
REQUESTS_PER_SECOND = 5
SCHEDULE_CHECK_SECONDS = 60


@dataclass
class AccountSchedule:
    profile_due: float = 0
    battle_due: float = 0
    battle_interval: float = INITIAL_POLL_SECONDS
    clan_tag: str | None = None
    player_name: str | None = None


class RankedStats(commands.Cog):
    def __init__(self, bot, account_links):
        self.bot = bot
        self.account_links = account_links
        self.client = bot.clash_client
        self.store = RankedStore(bot.paths.data_root / "ranked_stats" / "ranked.sqlite3")
        self.queries = RankedQueries(self.store)
        self._missing_fields = set()
        self._collect_lock = asyncio.Lock()
        self._schedules = {}
        self._request_lock = asyncio.Lock()
        self._request_times = deque(maxlen=REQUESTS_PER_SECOND)

    async def cog_load(self):
        await asyncio.to_thread(self.store.initialize)
        start_resilient_loop(self.snapshot_loop)

    def cog_unload(self):
        self.snapshot_loop.cancel()

    def _report_field(self, tag, field, missing):
        key = (tag, field)
        if missing and key not in self._missing_fields:
            LOGGER.warning("Ranked snapshot skips missing or invalid player field %s for %s", field, tag)
            self._missing_fields.add(key)
        elif not missing:
            self._missing_fields.discard(key)

    async def collect_once(self):
        if not self.client.configured:
            return
        async with self._collect_lock:
            started = monotonic()
            links = await asyncio.to_thread(self.account_links.get_all_links)
            accounts = {}
            for raw_tag, link in links.items():
                tag = normalize_player_tag(raw_tag)
                member_id = link.get("discord_user_id")
                if tag is None or type(member_id) is not int or member_id <= 0:
                    LOGGER.warning("Ranked snapshot skips invalid linked account identity")
                    continue
                accounts[tag] = member_id
            tags = set(accounts)
            self._missing_fields = {key for key in self._missing_fields if key[0] in tags}
            self._schedules = {tag: schedule for tag, schedule in self._schedules.items() if tag in tags}
            await self._restore_schedules(tags - self._schedules.keys())
            semaphore = asyncio.Semaphore(4)

            async def collect(tag, member_id):
                async with semaphore:
                    schedule = self._schedules[tag]
                    row = {"player_tag": tag, "member_id": member_id, "clan_tag": schedule.clan_tag,
                           "player_name": schedule.player_name}
                    snapshot, poll, battles = None, None, []
                    if monotonic() >= schedule.profile_due:
                        snapshot = await self._collect_profile(row)
                        schedule.profile_due = monotonic() + PROFILE_INTERVAL_SECONDS
                        if snapshot["status"] == "ok":
                            schedule.clan_tag = snapshot["clan_tag"]
                            schedule.player_name = snapshot["player_name"]
                        row.update(clan_tag=schedule.clan_tag, player_name=schedule.player_name)
                    if monotonic() >= schedule.battle_due:
                        row["observed_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                        # The log remains useful when the profile request fails or is not due.
                        poll, battles = await self._collect_battles(row, schedule.battle_interval)
                        schedule.battle_interval = poll["poll_interval_seconds"]
                        schedule.battle_due = monotonic() + schedule.battle_interval
                    return snapshot, poll, battles

            completed = await asyncio.gather(*(collect(tag, member) for tag, member in accounts.items()
                if monotonic() >= min(self._schedules[tag].profile_due, self._schedules[tag].battle_due)))
            if completed:
                await asyncio.to_thread(self.store.append,
                    [row for row, _, _ in completed if row is not None],
                    polls=[poll for _, poll, _ in completed if poll is not None],
                    battles=[battle for _, _, battles in completed for battle in battles])
            # Wake for the next deadline, checking at least once a minute for link changes.
            now = monotonic()
            next_due = min((min(s.profile_due, s.battle_due) for s in self._schedules.values()),
                           default=now + SCHEDULE_CHECK_SECONDS)
            delay = max(1, min(SCHEDULE_CHECK_SECONDS, next_due - now))
            # discord's loop interval is measured from the start of this iteration.
            self.snapshot_loop.change_interval(seconds=now - started + delay)

    async def _restore_schedules(self, tags):
        if not tags:
            return
        latest = await asyncio.to_thread(self.store.latest_collection, tags)
        now, wall_now = monotonic(), datetime.now(timezone.utc)
        for tag, (profile, poll) in latest.items():
            schedule = AccountSchedule()
            if profile:
                remaining = PROFILE_INTERVAL_SECONDS - (wall_now - battle_time(profile["observed_at"])).total_seconds()
                schedule.profile_due = now + max(0, min(PROFILE_INTERVAL_SECONDS, remaining))
                if profile["status"] == "ok":
                    schedule.clan_tag, schedule.player_name = profile["clan_tag"], profile["player_name"]
            if poll:
                schedule.battle_interval = max(MIN_POLL_SECONDS, min(MAX_POLL_SECONDS, poll["poll_interval_seconds"]))
                remaining = schedule.battle_interval - (wall_now - battle_time(poll["observed_at"])).total_seconds()
                schedule.battle_due = now + max(0, min(schedule.battle_interval, remaining))
            self._schedules[tag] = schedule

    async def _get(self, path):
        # One rolling-window budget covers both endpoint types and all accounts.
        async with self._request_lock:
            while len(self._request_times) == REQUESTS_PER_SECOND:
                delay = self._request_times[0] + 1 - monotonic()
                if delay <= 0:
                    break
                await asyncio.sleep(delay)
            self._request_times.append(monotonic())
        return await self.client.get(path, attempts=1)

    async def _collect_profile(self, identity):
        tag = identity["player_tag"]
        row = {**identity, "clan_tag": None, "player_name": None, "status": "unavailable", "fields": {}}
        try:
            response = await self._get("/players/" + encode_clash_tag(tag))
            payload = response.payload
            if response.ok and isinstance(payload, dict):
                returned_tag = normalize_player_tag(payload.get("tag"))
                if returned_tag is not None and returned_tag != tag:
                    raise ValueError("Player response identity mismatch")
                row["status"] = "ok"
                row["fields"] = project_player(payload, lambda field, missing: self._report_field(tag, field, missing))
                self._report_field(tag, "tag", returned_tag is None)
                name = payload.get("name")
                if isinstance(name, str):
                    row["player_name"] = name
                self._report_field(tag, "name", not isinstance(name, str))
                clan = payload.get("clan")
                if isinstance(clan, dict):
                    row["clan_tag"] = normalize_player_tag(clan.get("tag"))
                    self._report_field(tag, "clan.tag", row["clan_tag"] is None)
            else:
                LOGGER.warning("Ranked player snapshot unavailable for %s (status %s)", tag, response.status)
        except Exception:
            LOGGER.exception("Ranked player snapshot failed for %s", tag)
        row["observed_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        return row

    async def _collect_battles(self, row, interval):
        tag = row["player_tag"]
        poll = {**{field: row[field] for field in ("player_tag", "observed_at", "member_id", "clan_tag")},
                "status": "unavailable", "total_entries": None, "oldest_battle_at": None,
                "newest_battle_at": None, "skipped_entries": 0,
                "poll_interval_seconds": interval}
        try:
            response = await self._get("/players/" + encode_clash_tag(tag) + "/battlelog")
            if response.ok:
                poll["observed_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                poll, battles = parse_log(response.payload, {**row, "observed_at": poll["observed_at"]},
                    lambda field, missing: self._report_field(tag, field, missing))
                return poll, battles
            LOGGER.warning("Ranked battle log unavailable for %s (status %s)", tag, response.status)
        except Exception:
            LOGGER.exception("Ranked battle log failed for %s", tag)
        poll["observed_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        return poll, []

    @tasks.loop(seconds=SCHEDULE_CHECK_SECONDS)
    async def snapshot_loop(self):
        await self.collect_once()

    @snapshot_loop.before_loop
    async def before_snapshot_loop(self):
        await self.bot.wait_until_ready()
        boot_complete = getattr(self.bot, "boot_complete", None)
        if boot_complete is not None:
            await boot_complete.wait()
