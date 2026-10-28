"""Collect existing sources without changing any feature's source of truth."""

import asyncio
from datetime import datetime
import logging

import discord
from discord.ext import commands

from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.infrastructure.clash import ClashClient
from elbow_helper.data.store import FamilyStore, now_us
from elbow_helper.data.store.clans import collect_clans
from elbow_helper.data.store.people import mirror_links, observe_discord


LOGGER = logging.getLogger(__name__)
# Nine clan profiles x 96 polls = 864 logical GETs/day before
# shared Cache-Control reuse. This collector never fetches player profiles.
CLAN_INTERVAL_SECONDS = 15 * 60
LINK_INTERVAL_SECONDS = 5 * 60
DISCORD_INTERVAL_SECONDS = 24 * 60 * 60
SCHEDULE_CHECK_SECONDS = 30


def _timestamp(value: datetime | None) -> int | None:
    return int(value.timestamp() * 1_000_000) if value is not None else None


def _member_row(member) -> dict:
    # Copy gateway metadata only. Never accept message objects or content.
    return {
        "id": member.id, "username": member.name, "display_name": member.display_name,
        "nick": member.nick, "bot": member.bot, "joined_at": _timestamp(member.joined_at),
        "roles": [role.id for role in member.roles if role.id != member.guild.id],
    }


class FamilyCollectors(commands.Cog):
    """Own schedules and gateway listeners; contain every collection failure."""

    def __init__(self, bot, store: FamilyStore, clash_client: ClashClient, clans):
        self.bot = bot
        self.store = store
        self.clash_client = clash_client
        self.clans = tuple(clans)
        self._tasks: list[asyncio.Task] = []
        self._discord_lock = asyncio.Lock()
        self._clan_lock = asyncio.Lock()
        self._link_lock = asyncio.Lock()
        self._closing = False

    async def cog_load(self):
        try:
            await self.store.initialize()
        except Exception:
            # An unavailable database cannot truthfully hold a failed run.
            LOGGER.warning("Family record initialization failed; collectors will retry")
        self._tasks = [
            asyncio.create_task(self._schedule("clans", CLAN_INTERVAL_SECONDS, self.collect_clans)),
            asyncio.create_task(self._schedule("links", LINK_INTERVAL_SECONDS, self.collect_links)),
            asyncio.create_task(self._schedule("discord", DISCORD_INTERVAL_SECONDS, self.scan_discord, startup=True)),
        ]

    async def cog_unload(self):
        self._closing = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        # Listener callbacks are dispatched as separate Discord tasks. Wait for
        # their in-flight transactions before allowing the client to close.
        async with self._discord_lock, self._clan_lock, self._link_lock:
            pass

    async def _schedule(self, name, seconds, collect, *, startup=False):
        await self.bot.boot_complete.wait()
        while not self._closing:
            try:
                if startup or await self.store.due(name, seconds, now_us()):
                    startup = False
                    await collect()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning("Family %s scheduling failed; will retry", name)
            await asyncio.sleep(SCHEDULE_CHECK_SECONDS)

    async def _run(self, collector, scope_kind, specs, operation):
        run = None
        error = None
        cancelled = False
        report = None
        try:
            run = await self.store.begin_run(collector, scope_kind, specs, now_us())
            await operation(run)
        except asyncio.CancelledError:
            error = "interrupted"
            cancelled = True
        except Exception:
            error = "collector_error"
        finally:
            if run is not None:
                try:
                    report = await self.store.finish(run, error=error)
                except Exception:
                    error = "store_unavailable"
            if error or report and report["failed"]:
                LOGGER.warning("Family %s collection failed or partial (run=%s, failed=%s, code=%s)",
                               collector, run, report["failed"] if report else "unknown", error or "item_failure")
        if cancelled:
            raise asyncio.CancelledError
        return report

    async def collect_clans(self):
        async with self._clan_lock:
            if self._closing:
                return
            specs = [(kind, clan.tag) for clan in self.clans for kind in ("clan_profile", "clan_roster")]

            async def operation(run):
                # Sequential clans bound fanout and stay within the shared
                # client cache/limiter. Each profile includes its memberList;
                # the two validation items share that one fetch's diagnostics.
                # A failed clan does not abort its siblings.
                responses = []
                for index, clan in enumerate(self.clans):
                    await self.store.requested(run, index * 2)
                    await self.store.requested(run, index * 2 + 1)
                    try:
                        responses.append(await self.clash_client.get(f"/clans/{encode_clash_tag(clan.tag)}"))
                    except Exception:
                        responses.append(None)
                await self.store.call(collect_clans, run, self.clans, responses, now_us())

            return await self._run("clans", "family_clans", specs, operation)

    async def collect_links(self):
        async with self._link_lock:
            if self._closing:
                return

            async def operation(run):
                await self.store.requested(run, 0)
                source = self.bot.get_cog("AccountLinks")
                if source is None:
                    raise RuntimeError("account_links_unavailable")
                # The feature's service returns one consistent SQLite snapshot.
                snapshot = await asyncio.to_thread(source.get_all_links)
                await self.store.call(mirror_links, run, snapshot, now_us())

            return await self._run("links", "current_links", [("account_links", "current")], operation)

    async def scan_discord(self):
        async with self._discord_lock:
            if self._closing:
                return
            scope = str(self.bot.guild_id)

            async def operation(run):
                await self.store.requested(run, 0)
                guild = self.bot.get_guild(self.bot.guild_id)
                if guild is None:
                    raise RuntimeError("guild_unavailable")
                rows = []
                complete = False
                before = guild.member_count
                try:
                    async for member in guild.fetch_members(limit=None):
                        rows.append(_member_row(member))
                    complete = type(before) is int and before == guild.member_count == len(rows)
                except (discord.HTTPException, OSError, ValueError):
                    # Completed pages remain positive observations. Missing
                    # pages never turn a scan into evidence of departure.
                    complete = False
                await self.store.call(observe_discord, run, rows, now_us(), complete=complete, scope=scope)

            return await self._run("discord", "guild", [("discord_scan", scope)], operation)

    async def _gateway(self, member, kind):
        if member.guild.id != self.bot.guild_id or self._closing:
            return
        async with self._discord_lock:
            if self._closing:
                return

            async def operation(run):
                await self.store.requested(run, 0)
                await self.store.call(
                    observe_discord, run, [] if kind == "leave" else [_member_row(member)], now_us(),
                    basis=f"gateway_{kind}", departed_id=member.id if kind == "leave" else None,
                    scope=str(self.bot.guild_id),
                )

            return await self._run("discord_event", "member", [(f"discord_{kind}", str(member.id))], operation)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        await self._gateway(member, "join")

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload):
        if payload.guild_id == self.bot.guild_id:
            # Raw removal also covers a member absent from the local cache.
            from types import SimpleNamespace
            await self._gateway(SimpleNamespace(id=payload.user.id, guild=SimpleNamespace(id=payload.guild_id)), "leave")

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        await self._gateway(after, "update")

    @commands.Cog.listener()
    async def on_user_update(self, before, after):
        guild = self.bot.get_guild(self.bot.guild_id)
        member = guild.get_member(after.id) if guild is not None else None
        if member is not None:
            await self._gateway(member, "update")
