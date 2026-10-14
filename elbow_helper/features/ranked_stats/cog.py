"""Five-minute snapshots of every linked account using the shared Clash client."""

import asyncio
from datetime import datetime, timezone
import logging

from discord.ext import commands, tasks

from elbow_helper.core.background import start_resilient_loop
from elbow_helper.domain.player_tags import encode_clash_tag, normalize_player_tag
from .fields import project_player
from .queries import RankedQueries
from .store import RankedStore


LOGGER = logging.getLogger(__name__)
POLL_MINUTES = 5


class RankedStats(commands.Cog):
    def __init__(self, bot, account_links):
        self.bot = bot
        self.account_links = account_links
        self.client = bot.clash_client
        self.store = RankedStore(bot.paths.data_root / "ranked_stats" / "ranked.sqlite3")
        self.queries = RankedQueries(self.store)
        self._missing_fields = set()
        self._collect_lock = asyncio.Lock()

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
                    return row

            rows = await asyncio.gather(*(collect(tag, link) for tag, link in links.items()))
            await asyncio.to_thread(self.store.append, [row for row in rows if row is not None])

    @tasks.loop(minutes=POLL_MINUTES)
    async def snapshot_loop(self):
        await self.collect_once()

    @snapshot_loop.before_loop
    async def before_snapshot_loop(self):
        await self.bot.wait_until_ready()
        boot_complete = getattr(self.bot, "boot_complete", None)
        if boot_complete is not None:
            await boot_complete.wait()
