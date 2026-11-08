"""Offline collector scheduling, lifecycle, failure containment and gateways."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tests.temp_directory import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.app import create_bot
from elbow_helper.core.paths import ApplicationPaths
from elbow_helper.data.collectors.service import FamilyCollectors
from tests.data.store_fixtures import empty_store
from elbow_helper.infrastructure.clash import ClashClient

from .test_family_store import CLANS, clan_response, player


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
        self.store = await empty_store(Path(self.temporary.name) / 'family.sqlite3')
        self.guild = FakeGuild()
        self.bot = SimpleNamespace(guild_id=123, boot_complete=asyncio.Event(),
                                   get_guild=lambda guild_id: self.guild)
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

    async def test_a_clan_failing_again_stays_quiet_until_it_changes_or_recovers(self):
        service = "elbow_helper.data.collectors.service"
        good = [clan_response(0, [player()]), clan_response(1, [])]
        self.client.get.side_effect = [TimeoutError("private detail"), good[1]]
        with self.assertLogs(service, level="WARNING") as first:
            await self.collectors.collect_clans()
        self.assertEqual(len(first.output), 1)
        self.assertIn("clan_profile #PY (fetch_failed)", first.output[0])
        self.assertNotIn("private detail", first.output[0])
        self.client.get.side_effect = [TimeoutError("private detail"), good[1]]
        with self.assertNoLogs(service, level="INFO"):
            report = await self.collectors.collect_clans()
        self.assertEqual(report["failed"], 2)
        self.client.get.side_effect = [good[0], clan_response(1, [])]
        with self.assertLogs(service, level="INFO") as recovered:
            report = await self.collectors.collect_clans()
        self.assertEqual(report["failed"], 0)
        self.assertEqual(len(recovered.output), 1)
        self.assertIn("recovered", recovered.output[0])
        self.client.get.side_effect = [TimeoutError("private detail"), good[1]]
        with self.assertLogs(service, level="WARNING") as again:
            await self.collectors.collect_clans()
        self.assertEqual(len(again.output), 1)


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
        self.assertNotIn("on_presence_update", kinds)

    async def test_last_messages_keep_only_who_where_and_when_and_survive_a_failed_save(self):
        class Message:
            # Reading anything but the author, guild, channel and time fails the test.
            def __init__(self, author_id, channel_id, at, *, bot=False, guild_id=123):
                self.author = SimpleNamespace(id=author_id, bot=bot)
                self.guild = SimpleNamespace(id=guild_id)
                self.channel = SimpleNamespace(id=channel_id)
                self.created_at = datetime.fromtimestamp(at, timezone.utc)

            def __getattr__(self, name):
                raise AssertionError(f"read message {name}")

        for message in (Message(100, 7, 1_000), Message(100, 8, 2_000), Message(100, 9, 1_500),
                        Message(101, 7, 1_200), Message(102, 7, 3_000, bot=True),
                        Message(103, 7, 3_000, guild_id=999)):
            await self.collectors.on_message(message)
        self.assertEqual(self.collectors._messages, {100: (2_000_000_000, 8), 101: (1_200_000_000, 7)})
        with patch("elbow_helper.data.collectors.service.observe_messages", side_effect=OSError("disk full")):
            with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING"):
                failed = await self.collectors.save_messages()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(await self.rows("SELECT * FROM member_last_messages"), [])
        # A newer message held while the save failed wins; the failed save's rows are kept.
        await self.collectors.on_message(Message(101, 9, 4_000))
        self.assertEqual(self.collectors._messages, {100: (2_000_000_000, 8), 101: (4_000_000_000, 9)})
        report = await self.collectors.save_messages()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(self.collectors._messages, {})
        await self.collectors.on_message(Message(100, 7, 1_800))
        await self.collectors.save_messages()
        rows = await self.rows("SELECT m.discord_id,l.message_at,l.channel_id FROM member_last_messages l "
                               "JOIN members m ON m.id=l.member_id ORDER BY m.discord_id")
        # An older sighting saved later never replaces a newer one.
        self.assertEqual([tuple(row.values()) for row in rows], [(100, 2_000_000_000, 8), (101, 4_000_000_000, 9)])
        self.assertIsNone(await self.collectors.save_messages())

    async def last_messages(self):
        return [tuple(row.values()) for row in await self.rows(
            "SELECT m.discord_id,l.message_at,l.channel_id,l.basis FROM member_last_messages l "
            "JOIN members m ON m.id=l.member_id ORDER BY m.discord_id")]


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
            report = await self.collectors.scan_discord()
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
            await self.collectors.scan_discord()
            await self.collectors.scan_discord()
            await self.collectors.on_member_join(member(self.guild))
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)

    async def test_store_finalize_failure_is_contained_and_warned_once(self):
        with (
            patch.object(self.store, "finish", new=AsyncMock(side_effect=OSError("unavailable"))),
            self.assertLogs("elbow_helper.data.collectors.service", level="WARNING") as logs,
        ):
            await self.collectors.scan_discord()
        self.assertEqual(len(logs.output), 1)

    async def test_persisted_schedule_skips_early_restart_and_startup_scan_is_forced(self):
        self.bot.boot_complete.set()
        collect = AsyncMock()

        async def stop_sleep(seconds):
            self.collectors._closing = True

        with patch.object(self.store, "due", new=AsyncMock(return_value=False)), patch(
            "elbow_helper.data.collectors.service.asyncio.sleep", new=stop_sleep,
        ):
            await self.collectors._schedule("discord", 86400, collect)
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
        self.assertFalse((paths.data_root / "family.sqlite3").exists())
        self.assertIs(bot.family_collectors.clash_client, client)
        self.assertTrue(bot.intents.members)
        self.assertFalse(bot.intents.presences)
        with (
            patch("elbow_helper.core.lifecycle.load_extensions", new=AsyncMock(return_value=SimpleNamespace())),
            patch("elbow_helper.core.lifecycle.sync_guild_commands", new=AsyncMock(return_value=())),
        ):
            await bot.setup_hook()
        self.assertIs(bot.get_cog("FamilyCollectors"), bot.family_collectors)
        # Ten lasting collector schedules.
        self.assertEqual(len(bot.family_collectors._tasks), 10)
        self.assertEqual(len(bot.family_collectors.clans), 9)
        self.assertTrue((paths.data_root / "family.sqlite3").is_file())
        for name in ("on_member_join", "on_member_update", "on_raw_member_remove", "on_user_update", "on_message"):
            self.assertIn(name, bot.extra_events)
        await bot.close()
        self.assertEqual(bot.family_collectors._tasks, [])
