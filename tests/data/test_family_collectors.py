"""Offline collector scheduling, lifecycle, failure containment and gateways."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.app import create_bot
from elbow_helper.core.paths import ApplicationPaths
from elbow_helper.data.collectors.service import FamilyCollectors, LINK_INTERVAL_SECONDS
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.projections import DONATION_FIELDS, roster_projection
from elbow_helper.infrastructure.clash import ClashClient
from tests.agent.test_compact_clash import synthetic_clan

from .test_family_store import CLANS, clan_response, link, player, response


class FakeGuild:
    id = 123

    def __init__(self):
        self.members = []
        self.member_count = 0
        self.fail = False

    async def fetch_members(self, *, limit):
        for member in self.members:
            yield member
        if self.fail:
            raise OSError("synthetic member fetch failure")

    def get_member(self, member_id):
        return next((row for row in self.members if row.id == member_id), None)


def member(guild, *, member_id=100, name="Username", nick="Nickname", roles=(10, 20)):
    return SimpleNamespace(id=member_id, name=name, nick=nick, display_name=nick or name,
                           bot=False, joined_at=datetime(2026, 1, 1, tzinfo=timezone.utc), guild=guild,
                           roles=[SimpleNamespace(id=role_id) for role_id in (guild.id, *roles)])


class FamilyCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.store = FamilyStore(Path(self.temporary.name) / "family.sqlite3")
        await self.store.initialize()
        self.guild = FakeGuild()
        self.source_thread = None

        def get_links():
            self.source_thread = threading.get_ident()
            return link()

        self.bot = SimpleNamespace(guild_id=123, boot_complete=asyncio.Event(),
                                   get_guild=lambda guild_id: self.guild,
                                   get_cog=lambda name: SimpleNamespace(get_all_links=get_links))
        self.client = SimpleNamespace(get=AsyncMock(side_effect=[clan_response(0, [player()]), clan_response(1, [])]))
        self.collectors = FamilyCollectors(self.bot, self.store, self.client, CLANS)

    async def rows(self, sql):
        return await self.store.call(lambda c: [dict(row) for row in c.execute(sql)])

    async def test_clan_profile_request_supplies_both_validation_items_through_shared_cache(self):
        report = await self.collectors.collect_clans()
        self.assertEqual(report, {"status": "ok", "succeeded": 3, "empty": 1, "failed": 0})
        calls = self.client.get.await_args_list
        self.assertEqual([call.args[0] for call in calls], ["/clans/%23PY", "/clans/%23LQ"])
        self.assertTrue(all(not call.kwargs for call in calls))
        self.assertEqual((await self.rows("SELECT expected FROM collection_runs"))[0]["expected"], 4)
        items = await self.rows("SELECT * FROM collection_items ORDER BY ordinal")
        self.assertEqual([row["source_kind"] for row in items], ["clan_profile", "clan_roster"] * 2)
        self.assertEqual([row["http_status"] for row in items], [200] * 4)

    async def test_existing_clan_profile_fixture_supplies_all_roster_fields_without_members_request(self):
        # Reuse the detailed profile fixture already exercised by the agent.
        # AccountLinks._fetch_clan_members and clan_health._validated_clan_members
        # likewise consume memberList directly from the clan profile endpoint.
        payload = synthetic_clan()
        required = {"tag", "name", "role", "expLevel", "league", "townHallLevel",
                    "leagueTier", "builderBaseLeague", "playerHouse", *DONATION_FIELDS}
        for row in payload["memberList"]:
            self.assertTrue(required <= row.keys())
        rows, complete = roster_projection(payload, payload["members"])
        self.assertTrue(complete)
        self.assertEqual(rows, payload["memberList"])
        self.collectors.clans = (SimpleNamespace(code="ONE", tag=payload["tag"], name=payload["name"]),)
        self.client.get.side_effect = None
        self.client.get.return_value = response(payload)
        report = await self.collectors.collect_clans()
        self.assertEqual(report, {"status": "ok", "succeeded": 2, "failed": 0, "empty": 0})
        self.client.get.assert_awaited_once_with(f"/clans/%23{payload['tag'][1:]}")
        samples = await self.rows(
            "SELECT a.player_tag,a.current_name,m.clan_role,d.* FROM donation_snapshots d "
            "JOIN accounts a ON a.id=d.account_id JOIN clan_membership m ON m.account_id=a.id")
        by_tag = {row["player_tag"]: row for row in samples}
        self.assertEqual(len(by_tag), payload["members"])
        for raw in payload["memberList"]:
            stored = by_tag[raw["tag"]]
            self.assertEqual((stored["current_name"], stored["clan_role"]), (raw["name"], raw["role"]))
            self.assertEqual([stored[key] for key in ("donations", "received", "trophies", "builder_trophies",
                                                      "clan_rank", "previous_rank")],
                             [raw[key] for key in DONATION_FIELDS])

    async def test_link_polling_schedule_is_five_minutes(self):
        with patch.object(self.collectors, "_schedule", new=AsyncMock()) as schedule:
            await self.collectors.cog_load()
            await asyncio.gather(*self.collectors._tasks)
        self.assertEqual(LINK_INTERVAL_SECONDS, 300)
        intervals = {call.args[0]: call.args[1] for call in schedule.await_args_list}
        self.assertEqual(intervals, {"clans": 900, "links": 300, "players": 3600, "wars": 30, "discord": 86400,
                                     "battle_logs": 30, "league_history": 30, "ranked_groups": 30,
                                     "capital_raids": 30, "reference_data": 30})
        await self.collectors.cog_unload()

    async def test_individual_fetch_exceptions_do_not_abort_remaining_clans_one_warning(self):
        self.client.get.side_effect = [TimeoutError("private detail"), clan_response(1, [])]
        with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING") as logs:
            report = await self.collectors.collect_clans()
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(self.client.get.await_count, 2)
        self.assertEqual(report["failed"], 2)
        items = await self.rows("SELECT * FROM collection_items ORDER BY ordinal")
        self.assertEqual([row["error_code"] for row in items[:2]], ["fetch_failed", "fetch_failed"])
        self.assertNotIn("private detail", str(items))

    async def test_link_service_runs_in_worker_and_failure_retains_ownership(self):
        await self.collectors.collect_links()
        self.assertNotEqual(self.source_thread, threading.get_ident())
        self.bot.get_cog = lambda name: None
        with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING") as logs:
            report = await self.collectors.collect_links()
        self.assertEqual(report["failed"], 1)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM account_links WHERE to_at IS NULL")), 1)

    async def test_startup_and_daily_scan_fetch_all_members_empty_scan_closes_members(self):
        self.guild.members = [member(self.guild)]
        self.guild.member_count = 1
        report = await self.collectors.scan_discord()
        self.assertEqual(report["status"], "ok")
        self.assertEqual([row["role_id"] for row in await self.rows("SELECT * FROM member_roles ORDER BY role_id")], [10, 20])
        self.guild.members = []
        self.guild.member_count = 0
        report = await self.collectors.scan_discord()
        self.assertEqual(report["status"], "empty")
        self.assertEqual(await self.rows("SELECT * FROM guild_membership WHERE to_at IS NULL"), [])

    async def test_failed_member_scan_is_partial_and_keeps_last_good_absent_members(self):
        self.guild.members = [member(self.guild), member(self.guild, member_id=200)]
        self.guild.member_count = 2
        await self.collectors.scan_discord()
        self.guild.members = self.guild.members[:1]
        self.guild.fail = True
        with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING") as logs:
            report = await self.collectors.scan_discord()
        self.assertEqual(report["failed"], 1)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM guild_membership WHERE to_at IS NULL")), 2)

    async def test_declared_member_count_mismatch_and_duplicate_ids_cannot_establish_absence(self):
        self.guild.members = [member(self.guild)]
        self.guild.member_count = 1
        await self.collectors.scan_discord()
        self.guild.members = []
        with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING"):
            await self.collectors.scan_discord()
        self.guild.members = [member(self.guild), member(self.guild)]
        self.guild.member_count = 2
        with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING"):
            await self.collectors.scan_discord()
        self.assertEqual(len(await self.rows("SELECT * FROM guild_membership WHERE to_at IS NULL")), 1)

    async def test_gateway_join_update_raw_leave_and_guild_filter(self):
        first = member(self.guild)
        await self.collectors.on_member_join(first)
        after = member(self.guild, name="Renamed", nick=None, roles=(20, 30))
        await self.collectors.on_member_update(first, after)
        await self.collectors.on_raw_member_remove(SimpleNamespace(guild_id=123, user=SimpleNamespace(id=100)))
        other = member(SimpleNamespace(id=999))
        await self.collectors.on_member_join(other)
        runs = await self.rows("SELECT * FROM collection_runs ORDER BY id")
        self.assertEqual(len(runs), 3)
        self.assertTrue(all(row["status"] == "ok" for row in runs))
        self.assertEqual((await self.rows("SELECT current_username,presence_status FROM members"))[0], {"current_username": "Renamed", "presence_status": "departed"})
        self.assertEqual(await self.rows("SELECT * FROM member_roles WHERE to_at IS NULL"), [])

    async def test_user_name_updates_are_collected_without_message_content(self):
        first = member(self.guild)
        self.guild.members = [first]
        await self.collectors.on_member_join(first)
        first.name = "Renamed"
        await self.collectors.on_user_update(SimpleNamespace(id=100), SimpleNamespace(id=100))
        self.assertEqual((await self.rows("SELECT current_username FROM members"))[0]["current_username"], "Renamed")
        kinds = [name for name, callback in self.collectors.get_listeners()]
        self.assertNotIn("on_message", kinds)
        self.assertNotIn("on_presence_update", kinds)

    async def test_shutdown_waits_for_scans_and_prevents_further_gateway_writes(self):
        self.bot.boot_complete.set()
        self.collectors._discord_lock = asyncio.Lock()
        await self.collectors._discord_lock.acquire()
        close = asyncio.create_task(self.collectors.cog_unload())
        await asyncio.sleep(0)
        self.assertFalse(close.done())
        self.collectors._discord_lock.release()
        await close
        await self.collectors.on_member_join(member(self.guild))
        self.assertEqual(await self.rows("SELECT * FROM collection_runs"), [])

    async def test_cancelled_fetch_finalizes_interrupted_run_with_unattempted_items_skipped(self):
        entered = asyncio.Event()

        async def waiting(path):
            entered.set()
            await asyncio.Event().wait()

        self.client.get.side_effect = waiting
        task = asyncio.create_task(self.collectors.collect_clans())
        await entered.wait()
        with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING"):
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        run = (await self.rows("SELECT * FROM collection_runs"))[0]
        self.assertEqual((run["status"], run["expected"], run["attempted"], run["failed"], run["skipped"]), ("interrupted", 4, 2, 2, 2))

    async def test_database_unavailable_is_one_external_warning_and_never_empty_success(self):
        with (
            patch.object(self.store, "begin_run", new=AsyncMock(side_effect=OSError("unavailable"))),
            self.assertLogs("elbow_helper.data.collectors.service", level="WARNING") as logs,
        ):
            report = await self.collectors.collect_links()
        self.assertIsNone(report)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(await self.rows("SELECT * FROM collection_runs"), [])

    async def test_all_sqlite_connections_from_collectors_use_worker_threads(self):
        import elbow_helper.data.store as store_module
        original = store_module.sqlite_connection
        threads = []

        def connect(*args, **kwargs):
            threads.append(threading.get_ident())
            return original(*args, **kwargs)

        with patch.object(store_module, "sqlite_connection", side_effect=connect):
            await self.collectors.collect_clans()
            await self.collectors.collect_links()
            await self.collectors.scan_discord()
            await self.collectors.on_member_join(member(self.guild))
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)

    async def test_store_finalize_failure_is_contained_and_warned_once(self):
        with (
            patch.object(self.store, "finish", new=AsyncMock(side_effect=OSError("unavailable"))),
            self.assertLogs("elbow_helper.data.collectors.service", level="WARNING") as logs,
        ):
            await self.collectors.collect_links()
        self.assertEqual(len(logs.output), 1)

    async def test_persisted_schedule_skips_early_restart_and_startup_scan_is_forced(self):
        self.bot.boot_complete.set()
        collect = AsyncMock()

        async def stop_sleep(seconds):
            self.collectors._closing = True

        with patch.object(self.store, "due", new=AsyncMock(return_value=False)), patch(
            "elbow_helper.data.collectors.service.asyncio.sleep", new=stop_sleep,
        ):
            await self.collectors._schedule("links", LINK_INTERVAL_SECONDS, collect)
            collect.assert_not_awaited()
            self.collectors._closing = False
            await self.collectors._schedule("discord", 86400, collect, startup=True)
            collect.assert_awaited_once()

    async def test_initialize_failure_does_not_abort_cog_loading(self):
        with (
            patch.object(self.store, "initialize", new=AsyncMock(side_effect=OSError("unavailable"))),
            self.assertLogs("elbow_helper.data.collectors.service", level="WARNING"),
        ):
            await self.collectors.cog_load()
        self.assertEqual(len(self.collectors._tasks), 10)
        await self.collectors.cog_unload()

    async def test_application_assembles_registers_collectors_and_stops_them_offline(self):
        paths = ApplicationPaths.from_project_root(Path(self.temporary.name))
        client = ClashClient(None)
        bot = create_bot(paths, client, SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
        self.assertFalse((paths.data_root / "family" / "family.sqlite3").exists())
        self.assertIs(bot.family_collectors.clash_client, client)
        self.assertTrue(bot.intents.members)
        self.assertFalse(bot.intents.presences)
        with (
            patch("elbow_helper.core.lifecycle.load_extensions", new=AsyncMock(return_value=SimpleNamespace())),
            patch("elbow_helper.core.lifecycle.sync_guild_commands", new=AsyncMock(return_value=())),
        ):
            await bot.setup_hook()
        self.assertIs(bot.get_cog("FamilyCollectors"), bot.family_collectors)
        self.assertEqual(len(bot.family_collectors._tasks), 9)
        self.assertEqual(len(bot.family_collectors.clans), 9)
        self.assertTrue((paths.data_root / "family" / "family.sqlite3").is_file())
        for name in ("on_member_join", "on_member_update", "on_raw_member_remove", "on_user_update"):
            self.assertIn(name, bot.extra_events)
        await bot.close()
        self.assertEqual(bot.family_collectors._tasks, [])
