"""Ranked snapshots and rolling battle logs through the shared Clash client."""

import asyncio
from datetime import datetime, timezone
import logging

from discord.ext import commands, tasks

from elbow_helper.core.background import start_resilient_loop
from elbow_helper.domain.player_tags import encode_clash_tag, normalize_player_tag
from .fields import project_player
from .queries import RankedQueries
from .store import RankedStore
from .battles import INITIAL_POLL_SECONDS, parse_log


LOGGER = logging.getLogger(__name__)


class RankedStats(commands.Cog):
    def __init__(self, bot, account_links):
        self.bot = bot
        self.account_links = account_links
        self.client = bot.clash_client
        self.store = RankedStore(bot.paths.data_root / "ranked_stats" / "ranked.sqlite3")
        self.queries = RankedQueries(self.store)
        self._missing_fields = set()
        self._collect_lock = asyncio.Lock()
        self._poll_intervals = {}

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
            links = await asyncio.to_thread(self.account_links.get_all_links)
            tags = {tag for value in links if (tag := normalize_player_tag(value))}
            self._missing_fields = {key for key in self._missing_fields if key[0] in tags}
            self._poll_intervals = {tag: interval for tag, interval in self._poll_intervals.items() if tag in tags}
            semaphore = asyncio.Semaphore(4)

            async def collect(raw_tag, link):
                tag = normalize_player_tag(raw_tag)
                member_id = link.get("discord_user_id")
                if tag is None or type(member_id) is not int or member_id <= 0:
                    LOGGER.warning("Ranked snapshot skips invalid linked account identity")
                    return None
                async with semaphore:
                    row = {"player_tag": tag, "member_id": member_id, "clan_tag": None,
                           "player_name": None, "status": "unavailable", "fields": {}}
                    try:
                        response = await self.client.get("/players/" + encode_clash_tag(tag), attempts=1)
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
                    # The log remains useful when the player profile request fails.
                    poll, battles = await self._collect_battles(row)
                    return row, poll, battles

            rows = await asyncio.gather(*(collect(tag, link) for tag, link in links.items()))
            completed = [result for result in rows if result is not None]
            await asyncio.to_thread(self.store.append,
                [row for row, _, _ in completed], polls=[poll for _, poll, _ in completed],
                battles=[battle for _, _, battles in completed for battle in battles])
            interval = min((self._poll_intervals.get(tag, INITIAL_POLL_SECONDS) for tag in tags),
                           default=INITIAL_POLL_SECONDS)
            self.snapshot_loop.change_interval(seconds=interval)

    async def _collect_battles(self, row):
        tag = row["player_tag"]
        poll = {**{field: row[field] for field in ("player_tag", "observed_at", "member_id", "clan_tag")},
                "status": "unavailable", "total_entries": None, "oldest_battle_at": None,
                "newest_battle_at": None, "skipped_entries": 0,
                "poll_interval_seconds": self._poll_intervals.get(tag, INITIAL_POLL_SECONDS)}
        try:
            response = await self.client.get("/players/" + encode_clash_tag(tag) + "/battlelog", attempts=1)
            if response.ok:
                poll["observed_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                poll, battles = parse_log(response.payload, {**row, "observed_at": poll["observed_at"]},
                    lambda field, missing: self._report_field(tag, field, missing))
                self._poll_intervals[tag] = poll["poll_interval_seconds"]
                return poll, battles
            LOGGER.warning("Ranked battle log unavailable for %s (status %s)", tag, response.status)
        except Exception:
            LOGGER.exception("Ranked battle log failed for %s", tag)
        poll["observed_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        return poll, []

    @tasks.loop(seconds=INITIAL_POLL_SECONDS)
    async def snapshot_loop(self):
        await self.collect_once()

    @snapshot_loop.before_loop
    async def before_snapshot_loop(self):
        await self.bot.wait_until_ready()
        boot_complete = getattr(self.bot, "boot_complete", None)
        if boot_complete is not None:
            await boot_complete.wait()
