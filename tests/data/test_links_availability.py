"""Legacy availability, cheap queries and deferred family-record switching."""

import asyncio
from contextlib import closing
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from elbow_helper.data.collectors.service import FamilyCollectors
from elbow_helper.data.link_facade import LinkFacade
from elbow_helper.data.links import LinkService
from elbow_helper.data.store import FamilyStore, now_us
from elbow_helper.data.store.legacy_links import LinkConflict

from .link_fixtures import CLANS, FrozenLegacyLinks
from .test_family_store import response
from .test_people_import import _PeopleFixture, ProcessCrash, row


class LinkAvailabilityTests(_PeopleFixture):
    async def test_clash_outage_uses_legacy_then_later_complete_generation_switches(self):
        await self.execute("UPDATE collection_items SET status='failed' WHERE source_kind='clan_roster'")
        client = SimpleNamespace(get=AsyncMock(return_value=response(None, 503)))
        bot = SimpleNamespace(link_service=self.service)
        collectors = FamilyCollectors(bot, self.store, client, CLANS.values())
        with patch("elbow_helper.data.links.LOGGER.warning") as logged:
            self.assertFalse((await self.service.startup(at=200))["mirrored"])
            self.assertFalse((await self.service.startup(at=201))["mirrored"])
            logged.assert_called_once()
        before = await self.service.read_links()
        await self.service.upsert_link(**row(owner=300))
        await self.service.delete_link("#Q0")
        await self.service.update_link_last_seen(player_tag="#P0", player_name_last_seen="Synthetic update",
            last_seen_clan_tag="", last_seen_clan_code="", last_seen_role="member")
        await self.service.restore_links(before)
        self.assertEqual(await self.service.read_links(), before)
        self.assertEqual(await self.rows("SELECT * FROM imports"), [])
        self.assertEqual(await self.rows("SELECT * FROM link_operations"), [])
        self.assertEqual(await self.rows("SELECT * FROM account_links"), [])
        with patch.object(self.service, "retry_import", wraps=self.service.retry_import) as retry:
            with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING"):
                await collectors.collect_clans()
            retry.assert_not_called()
            client.get.side_effect = [response({"tag": c.tag, "name": c.name, "members": 0, "memberList": []})
                                      for c in CLANS.values()]
            await collectors.collect_clans()
            retry.assert_awaited_once()
        self.assertTrue(self.service._started)
        self.assertEqual(len(await self.rows("SELECT * FROM account_links WHERE to_at IS NULL")), 3)
        await self.service.upsert_link(**row(owner=400))
        self.assertEqual((await self.rows("SELECT status FROM link_operations"))[0]["status"], "complete")

    async def test_unavailable_family_record_never_blocks_legacy_reads_or_writes(self):
        before = await self.dump()
        with patch.object(self.store, "_call", side_effect=OSError("family unavailable")) as family:
            with self.assertLogs("elbow_helper.data.links", level="WARNING"):
                self.assertFalse((await self.service.startup())["mirrored"])
            self.assertEqual(len(await self.service.read_links()), 3)
            await self.service.upsert_link(**row("#V0", 300))
            self.assertEqual((await self.service.read_links())["#V0"]["discord_user_id"], 300)
            await self.service.delete_link("#V0")
            self.assertEqual(family.call_count, 1)
        self.assertEqual(await self.dump(), before)

    async def test_existing_marker_needs_no_roster_even_after_legacy_rollback(self):
        await self.service.startup(at=200)
        await self.execute("UPDATE collection_items SET status='failed' WHERE source_kind='clan_roster'")
        before = await self.dump()
        restarted = LinkService(FamilyStore(self.store.path), self.legacy)
        with patch("elbow_helper.data.store.people_import.roster_checks", side_effect=AssertionError("roster gate")):
            result = await restarted.startup(at=now_us())
            self.assertTrue(result["mirrored"])
            self.assertFalse(result["imported"])
            self.assertEqual(await self.dump(), before)
            with closing(sqlite3.connect(self.legacy)) as connection, connection:
                connection.execute("UPDATE links SET discord_user_id=300 WHERE player_tag='#P0'")
            result = await restarted.startup()
        self.assertTrue(result["mirrored"])
        self.assertTrue(result["imported"])
        self.assertEqual((await self.rows("SELECT basis FROM link_imports ORDER BY id DESC LIMIT 1"))[0]["basis"],
                         "rollback_gap")

    async def test_reads_and_startup_reads_do_not_queue_behind_long_collector(self):
        await self.service.startup(at=200)
        entered, release = threading.Event(), threading.Event()
        def collect(connection):
            with connection:
                connection.execute("UPDATE accounts SET tracking_status=tracking_status")
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test collector timeout")
        collector = asyncio.create_task(self.store.call(collect))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            facade = LinkFacade(self.service)
            for method, args in ((facade.by_tag, ("#P0",)), (facade.for_user, (100,)), (facade.all, ())):
                self.assertTrue(await asyncio.wait_for(asyncio.to_thread(method, *args), 1))
            startup = asyncio.create_task(self.service.startup(at=201))
            await asyncio.sleep(0)
            self.assertEqual(len(await asyncio.wait_for(self.service.read_links(), 1)), 3)
            self.assertFalse(collector.done())
        finally:
            release.set()
            await collector
        self.assertTrue((await startup)["mirrored"])

    async def test_reads_use_only_the_old_queries_without_integrity_or_mapping_scans(self):
        await self.service.startup(at=200)
        facade = LinkFacade(self.service)
        reference = FrozenLegacyLinks(self.legacy)
        queries = []
        connect = sqlite3.connect
        def traced(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(queries.append)
            return connection
        for method, old, args in ((facade.all, reference.get_all_links, ()),
                (facade.by_tag, reference.get_link_by_tag, ("#P0",)),
                (facade.by_tags, reference.get_links_by_tags, (["#P0", "#V0"],)),
                (facade.for_user, reference.get_links_for_user, (100,)),
                (facade.for_members, reference.get_links_for_members, ([100, 200, 999],))):
            with self.subTest(method=method.__name__):
                expected = await asyncio.to_thread(old, *args)
                queries.clear()
                with (patch("elbow_helper.data.links.sqlite3.connect", side_effect=traced),
                      patch("elbow_helper.data.links.snapshot", side_effect=AssertionError("snapshot on read")),
                      patch.object(self.store, "_call", side_effect=AssertionError("family on read"))):
                    actual = await asyncio.to_thread(method, *args)
                self.assertEqual(actual, expected)
                self.assertEqual(len([q for q in queries if q.startswith("SELECT")]), 1)
                self.assertFalse(any("PRAGMA" in q or "CREATE" in q for q in queries))

    async def test_ordinary_writes_do_not_validate_the_whole_mapping(self):
        await self.service.startup(at=200)
        with (patch("elbow_helper.data.links.snapshot", side_effect=AssertionError("snapshot on write")),
              patch("elbow_helper.data.links.check_mapping", side_effect=AssertionError("mapping scan on write"))):
            await self.service.upsert_links([row(owner=300), row("#V0", 300)])
            await self.service.delete_link("#Q0")
            await self.service.update_link_last_seen(player_tag="#P0", player_name_last_seen="Synthetic update",
                last_seen_clan_tag="", last_seen_clan_code="", last_seen_role="member")
        self.assertTrue(self.service._started)
        self.assertTrue(all(r["status"] == "complete" for r in await self.rows("SELECT status FROM link_operations")))

    async def test_legacy_commit_does_not_hold_the_family_writer(self):
        await self.service.startup(at=200)
        apply = self.service._apply_legacy
        def independent(*args, **kwargs):
            acquired = self.store._writer.acquire(blocking=False)
            self.assertTrue(acquired)
            if acquired:
                self.store._writer.release()
            return apply(*args, **kwargs)
        with patch.object(self.service, "_apply_legacy", independent):
            await self.service.upsert_link(**row(owner=300))

    async def test_family_finish_keeps_full_durability_on_its_separate_connection(self):
        await self.service.startup(at=200)
        from elbow_helper.data import links
        apply = links.apply_rows
        modes = []
        def durable(connection, *args, **kwargs):
            modes.append(connection.execute("PRAGMA synchronous").fetchone()[0])
            return apply(connection, *args, **kwargs)
        with patch.object(links, "apply_rows", durable):
            await self.service.upsert_link(**row(owner=300))
        self.assertEqual(modes, [2])

    async def test_transient_after_commit_error_recovers_before_releasing_readers(self):
        await self.service.startup(at=200)
        entered, release = threading.Event(), threading.Event()
        apply, finish = self.service._apply_legacy, self.service._finish
        attempts = []
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test write timeout")
            return apply(*args, **kwargs)
        def failed_once(*args, **kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise OSError("transient after legacy commit")
            return finish(*args, **kwargs)
        with patch.object(self.service, "_apply_legacy", blocked), patch.object(self.service, "_finish", failed_once):
            writer = asyncio.create_task(self.service.upsert_link(**row(owner=300)))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                reader = asyncio.create_task(self.service.read_links())
                await asyncio.sleep(0.02)
                self.assertFalse(reader.done())
            finally:
                release.set()
            with self.assertRaises(OSError):
                await writer
            self.assertEqual((await reader)["#P0"]["discord_user_id"], 300)
        self.assertTrue(self.service._started)
        self.assertEqual((await self.rows("SELECT status FROM link_operations"))[0]["status"], "complete")

    async def test_mixed_recovery_keeps_record_and_intent_unchanged_but_legacy_active(self):
        await self.service.startup(at=200)
        with patch.object(self.service, "_finish", side_effect=ProcessCrash()):
            with self.assertRaises(ProcessCrash):
                await self.service.upsert_links([row(owner=300), row("#Q0", 300)], at=300)
        with closing(sqlite3.connect(self.legacy)) as connection, connection:
            connection.execute("UPDATE links SET discord_user_id=100 WHERE player_tag='#Q0'")
        before = await self.dump()
        self.service = LinkService(FamilyStore(self.store.path), self.legacy)
        with self.assertLogs("elbow_helper.data.links", level="WARNING") as logs:
            result = await self.service.startup(at=400)
            self.assertFalse(result["mirrored"])
            self.assertIn("image_conflict", result["reason"])
            self.assertEqual((await self.service.read_links())["#P0"]["discord_user_id"], 300)
            await self.service.upsert_link(**row("#V0", 400))
            self.assertFalse((await self.service.retry_import())["mirrored"])
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(await self.dump(), before)
        self.assertEqual((await self.service.read_links())["#V0"]["discord_user_id"], 400)
        self.assertEqual((await self.rows("SELECT status FROM link_operations"))[0]["status"], "pending")
        with self.assertRaises(LinkConflict):
            await self.service.recover()

    async def test_intent_failure_falls_back_and_later_records_a_gap(self):
        await self.service.startup(at=200)
        before = await self.dump()
        with (patch.object(self.service, "_intent", side_effect=sqlite3.OperationalError("record unavailable")),
              self.assertLogs("elbow_helper.data.links", level="WARNING")):
            await self.service.upsert_link(**row(owner=300))
        self.assertEqual(await self.dump(), before)
        self.assertFalse(self.service._started)
        self.assertEqual((await self.service.read_links())["#P0"]["discord_user_id"], 300)
        self.assertTrue((await self.service.retry_import())["mirrored"])
        self.assertEqual((await self.rows("SELECT basis FROM link_imports ORDER BY id DESC LIMIT 1"))[0]["basis"],
                         "rollback_gap")
