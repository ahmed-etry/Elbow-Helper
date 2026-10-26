"""Ranked snapshots and rolling battle logs through the shared Clash client."""

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from time import monotonic

import aiohttp
from discord.ext import commands, tasks

from elbow_helper.core.background import start_resilient_loop
from elbow_helper.domain.player_tags import encode_clash_tag, normalize_player_tag
from .fields import project_player
from .queries import RankedQueries
from .store import RankedStore
from .health import CollectionHealth
from .battles import INITIAL_POLL_SECONDS, MAX_POLL_SECONDS, MIN_POLL_SECONDS, battle_time, log_identities, parse_log


LOGGER = logging.getLogger(__name__)
PROFILE_INTERVAL_SECONDS = 3600
MISSING_PROFILE_INTERVAL_SECONDS = 24 * 3600
REQUESTS_PER_SECOND = 5
SCHEDULE_CHECK_SECONDS = 60


@dataclass
class AccountSchedule:
    profile_due: float = 0
    battle_due: float = 0
    battle_interval: float = INITIAL_POLL_SECONDS
    clan_tag: str | None = None
    player_name: str | None = None
    previous_battles: set[str] | None = None
    missing: bool = False


class RankedStats(commands.Cog):
    def __init__(self, bot, account_links):
        self.bot = bot
        self.account_links = account_links
        self.client = bot.clash_client
        self.store = RankedStore(bot.paths.data_root / "ranked_stats" / "ranked.sqlite3")
        self.queries = RankedQueries(self.store)
        self._missing_fields = set()
        self._invalid_links = False
        self._health = CollectionHealth(LOGGER)
        self._collect_lock = asyncio.Lock()
        self._schedules = {}
        self._linked_accounts = None
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

    def _report_failure(self, tag, kind, status):
        self._health.record(tag, kind, status)

    async def collect_once(self):
        if not self.client.configured:
            return
        async with self._collect_lock:
            started = monotonic()
            links = await asyncio.to_thread(self.account_links.get_all_links)
            accounts = {}
            invalid_links = 0
            for raw_tag, link in links.items():
                tag = normalize_player_tag(raw_tag)
                member_id = link.get("discord_user_id")
                if tag is None or type(member_id) is not int or member_id <= 0:
                    invalid_links += 1
                    continue
                accounts[tag] = member_id
            if invalid_links and not self._invalid_links:
                LOGGER.warning("Ranked snapshot skips %s invalid linked account identities", invalid_links)
            elif not invalid_links and self._invalid_links:
                LOGGER.info("Ranked linked account identities recovered")
            self._invalid_links = bool(invalid_links)
            tags = set(accounts)
            self._missing_fields = {key for key in self._missing_fields if key[0] in tags}
            self._health.retain(tags)
            self._schedules = {tag: schedule for tag, schedule in self._schedules.items() if tag in tags}
            await self._restore_schedules(tags - self._schedules.keys())
            if self._linked_accounts is not None:
                for tag, member_id in accounts.items():
                    if self._schedules[tag].missing and self._linked_accounts.get(tag) != member_id:
                        # A newly linked or reassigned missing tag deserves an immediate retry.
                        self._schedules[tag].profile_due = 0
            self._linked_accounts = accounts
            semaphore = asyncio.Semaphore(4)

            async def collect(tag, member_id):
                async with semaphore:
                    schedule = self._schedules[tag]
                    row = {"player_tag": tag, "member_id": member_id, "clan_tag": schedule.clan_tag,
                           "player_name": schedule.player_name}
                    snapshot, poll, battles = None, None, []
                    if monotonic() >= schedule.profile_due:
                        snapshot = await self._collect_profile(row)
                        if snapshot["status"] == "ok":
                            if schedule.missing:
                                schedule.battle_due = 0
                                schedule.battle_interval = INITIAL_POLL_SECONDS
                                schedule.previous_battles = None
                            schedule.missing = False
                            schedule.clan_tag = snapshot["clan_tag"]
                            schedule.player_name = snapshot["player_name"]
                        elif snapshot["status"] == "not_found":
                            schedule.missing = True
                        interval = MISSING_PROFILE_INTERVAL_SECONDS if schedule.missing else PROFILE_INTERVAL_SECONDS
                        schedule.profile_due = monotonic() + interval
                        row.update(clan_tag=schedule.clan_tag, player_name=schedule.player_name)
                    if schedule.missing:
                        schedule.battle_due = schedule.profile_due
                    elif monotonic() >= schedule.battle_due:
                        row["observed_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                        # The log remains useful when the profile request fails or is not due.
                        poll, battles = await self._collect_battles(row, schedule.battle_interval)
                        schedule.battle_interval = poll["poll_interval_seconds"]
                        schedule.battle_due = monotonic() + schedule.battle_interval
                    return snapshot, poll, battles

            completed = await asyncio.gather(*(collect(tag, member) for tag, member in accounts.items()
                if monotonic() >= min(self._schedules[tag].profile_due, self._schedules[tag].battle_due)))
            self._health.report()
            if completed:
                await asyncio.to_thread(self.store.append,
                    [row for row, _, _ in completed if row is not None],
                    polls=[poll for _, poll, _ in completed if poll is not None],
                    battles=[battle for _, _, battles in completed for battle in battles],
                    profile_health=self._health.missing_state())
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
        states = await asyncio.to_thread(self.store.profile_health, tags)
        self._health.restore_missing(states)
        now, wall_now = monotonic(), datetime.now(timezone.utc)
        for tag, (profile, poll) in latest.items():
            schedule = AccountSchedule(missing=states.get(tag, False))
            if profile:
                interval = MISSING_PROFILE_INTERVAL_SECONDS if schedule.missing else PROFILE_INTERVAL_SECONDS
                remaining = interval - (wall_now - battle_time(profile["observed_at"])).total_seconds()
                schedule.profile_due = now + max(0, min(interval, remaining))
                if profile["status"] == "ok":
                    schedule.clan_tag, schedule.player_name = profile["clan_tag"], profile["player_name"]
            if poll:
                schedule.battle_interval = max(MIN_POLL_SECONDS, min(MAX_POLL_SECONDS, poll["poll_interval_seconds"]))
                remaining = schedule.battle_interval - (wall_now - battle_time(poll["observed_at"])).total_seconds()
                schedule.battle_due = now + max(0, min(schedule.battle_interval, remaining))
            if schedule.missing:
                schedule.battle_due = schedule.profile_due
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
                self._health.record(tag, "player snapshot", 200, ok=True)
            else:
                if response.status == 404:
                    row["status"] = "not_found"
                self._report_failure(tag, "player snapshot", response.status)
        except (TimeoutError, aiohttp.ClientError, OSError):
            self._report_failure(tag, "player snapshot", None)
        except Exception:
            self._report_failure(tag, "player snapshot", None)
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
                identities = log_identities(response.payload)
                schedule = self._schedules[tag]
                if identities and schedule.previous_battles and identities.isdisjoint(schedule.previous_battles):
                    LOGGER.warning(
                        "Ranked battle log for %s shares no battles with the previous poll; "
                        "battles may have been missed between polls", tag,
                    )
                schedule.previous_battles = identities
                self._health.record(tag, "battle log", 200, ok=True)
                return poll, battles
            self._report_failure(tag, "battle log", response.status)
        except (TimeoutError, aiohttp.ClientError, OSError):
            self._report_failure(tag, "battle log", None)
        except Exception:
            self._report_failure(tag, "battle log", None)
            LOGGER.exception("Ranked battle log failed for %s", tag)
        # Back off an account whose log keeps failing, up to the slowest poll interval.
        poll["poll_interval_seconds"] = min(MAX_POLL_SECONDS, interval * 2)
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
