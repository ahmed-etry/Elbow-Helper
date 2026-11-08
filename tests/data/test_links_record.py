"""The record is the sole source and atomic writer for current ownership."""

import asyncio
from contextlib import closing
from dataclasses import replace
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from elbow_helper.data.link_facade import LinkFacade
from elbow_helper.data.links import LinkService
from elbow_helper.data.reads import MembershipReads, PeopleReads
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.link_state import LinkConflict
from elbow_helper.features.account_links.cog import AccountLinks
from elbow_helper.agent.tool_support.adapters.account_links.data import SOURCES
from elbow_helper.agent.tool_support.sql.query import run_query

from .link_fixtures import CLANS
from .record_fixtures import PeopleFixture, ProcessCrash, row


class RecordLinkTests(PeopleFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog = AccountLinks(SimpleNamespace(link_service=self.service), SimpleNamespace(configured=False))
        self.cog._try_refresh_linked_boards = AsyncMock()
        self.cog.refresh_linked_boards = AsyncMock()

    async def test_neutral_current_links_carry_readiness_and_reject_historical_projection(self):
        reads = PeopleReads(self.store)
        result = await reads.current_links(200)
        self.assertEqual([row["player_tag"] for row in result["rows"]], ["#P0", "#Q0", "#R0"])
        self.assertTrue(result["coverage"]["complete"])
        old = await reads.current_links(199)
        self.assertEqual(old["rows"], [])
        self.assertEqual(old["coverage"]["unavailable"], "current_projection_only")
        await self.execute("INSERT INTO link_operations(operation_key,requested_json,before_json,after_json,"
                           "before_digest,after_digest,created_at,status) "
                           "VALUES ('old','{}','{}','{}','before','after',250,'pending')")
        pending = await reads.current_links(300)
        self.assertEqual(pending["rows"], [])
        self.assertFalse(pending["coverage"]["complete"])
        self.assertEqual(pending["coverage"]["unavailable"], "link_recovery_required")


    async def test_add_transfer_delete_refresh_and_undo_leave_old_file_untouched(self):
        original = self.legacy.read_bytes()
        before = await self.service.read_links()
        await self.service.upsert_link(**row("#V0", 300, "Alt"), at=250)
        await self.service.upsert_link(**row(owner=300, name="New"), at=300)
        await self.service.delete_link("#Q0", at=400)
        await self.service.update_link_last_seen(player_tag="#P0", player_name_last_seen="Fresh",
            last_seen_clan_tag=None, last_seen_clan_code="", last_seen_role=None, at=450)
        await self.service.restore_links({**before, "#V0": None}, at=500)
        self.assertEqual(await self.service.read_links(), before)
        self.assertEqual(self.legacy.read_bytes(), original)
        self.assertEqual([r["status"] for r in await self.rows("SELECT status FROM link_operations")], ["complete"] * 5)
        history = await PeopleReads(self.store).account_history(["#P0"], 600)
        self.assertEqual([r["member_id"] for r in history["rows"][0]["record_history"]["ownership_intervals"]],
                         [100, 300, 100])

    async def test_each_late_batch_failure_leaves_no_identity_history_receipt_or_metadata(self):
        for target, event in (("members", "INSERT"), ("account_links", "INSERT"),
                              ("account_link_metadata", "INSERT"), ("link_operations", "INSERT")):
            with self.subTest(target=target):
                await self.execute(f"CREATE TRIGGER refuse_write BEFORE {event} ON {target} "
                                   "BEGIN SELECT RAISE(ABORT,'synthetic refused write'); END")
                before = await self.dump()
                original = self.legacy.read_bytes()
                with self.assertRaises(sqlite3.IntegrityError):
                    await self.service.upsert_links([row(owner=300), row("#V0", 300, "Alt")], at=300)
                self.assertEqual(await self.dump(), before)
                self.assertEqual(self.legacy.read_bytes(), original)
                await self.execute("DROP TRIGGER refuse_write")

    async def test_crash_inside_transaction_rolls_back_and_restart_never_reads_legacy(self):
        before = await self.dump()
        with patch("elbow_helper.data.links.display_rows", side_effect=ProcessCrash()):
            with self.assertRaises(ProcessCrash):
                await self.service.upsert_link(**row("#V0", 300), at=300)
        self.assertEqual(await self.dump(), before)
        self.legacy.unlink()
        restart = LinkService(FamilyStore(self.store.path))
        await restart.upsert_link(**row("#V0", 300), at=300)
        self.assertEqual((await restart.read_links())["#V0"]["discord_user_id"], 300)
        self.assertFalse(self.legacy.exists())

    async def test_idempotent_retry_and_key_conflict(self):
        await self.service.upsert_link(**row("#V0", 300), operation_key="same", at=300)
        before = await self.dump()
        await self.service.upsert_link(**row("#V0", 300), operation_key="same", at=400)
        self.assertEqual(await self.dump(), before)
        with self.assertRaises(LinkConflict):
            await self.service.delete_link("#V0", operation_key="same", at=400)
        self.assertEqual(await self.dump(), before)

    async def test_same_instant_link_unlink_transfer_and_restore_keep_strict_order(self):
        before = await self.service.read_links()
        with patch("elbow_helper.data.links.now_us", return_value=200):
            await self.service.upsert_link(**row("#V0", 300), operation_key="link")
            await self.service.delete_link("#V0", operation_key="unlink")
            await self.service.upsert_link(**row("#V0", 400), operation_key="relink")
            await self.service.upsert_link(**row(owner=300), operation_key="transfer")
            # The ordering survives a new service and store, even while the clock is frozen.
            self.service = LinkService(FamilyStore(self.store.path))
            await self.service.restore_links({"#P0": before["#P0"], "#V0": None}, operation_key="restore")
        self.assertEqual(await self.service.read_links(), before)
        operations = await self.rows("SELECT o.operation_key,o.created_at,o.finished_at,i.committed_at "
                                     "FROM link_operations o JOIN collection_items i ON i.id=o.source_item_id "
                                     "ORDER BY o.created_at")
        self.assertEqual([(r["operation_key"], r["created_at"], r["finished_at"], r["committed_at"])
                          for r in operations],
                         [(key, at, at, at) for key, at in zip(
                             ("link", "unlink", "relink", "transfer", "restore"), range(201, 206))])
        history = await self.rows("SELECT a.player_tag,m.discord_id,l.from_at,l.to_at "
                                  "FROM account_links l JOIN accounts a ON a.id=l.account_id "
                                  "JOIN members m ON m.id=l.member_id "
                                  "WHERE a.player_tag IN ('#P0','#V0') ORDER BY a.player_tag,l.from_at")
        self.assertEqual([(r["player_tag"], r["discord_id"], r["from_at"], r["to_at"]) for r in history],
                         [("#P0", 100, 200, 204), ("#P0", 300, 204, 205), ("#P0", 100, 205, None),
                          ("#V0", 300, 201, 202), ("#V0", 400, 203, 205)])
        people = PeopleReads(self.store)
        for at, owners in ((201, [100, 300]), (202, [100, None]), (203, [100, 400]),
                           (204, [300, 400]), (205, [100, None])):
            self.assertEqual([r["member_id"] for r in (await people.owners(["#P0", "#V0"], at))["rows"]], owners)

    async def test_same_instant_failure_and_retry_do_not_consume_a_timestamp(self):
        with patch("elbow_helper.data.links.now_us", return_value=300):
            await self.service.upsert_link(**row(owner=300), operation_key="first")
            await self.execute("CREATE TRIGGER refuse_write BEFORE INSERT ON link_operations "
                               "BEGIN SELECT RAISE(ABORT,'synthetic refused write'); END")
            before, legacy = await self.dump(), self.legacy.read_bytes()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic refused write"):
                await self.service.upsert_link(**row(owner=400), operation_key="retry")
            self.assertEqual(await self.dump(), before)
            self.assertEqual(self.legacy.read_bytes(), legacy)
            await self.execute("DROP TRIGGER refuse_write")
            await self.service.upsert_link(**row(owner=400), operation_key="retry")
            before = await self.dump()
            await self.service.upsert_link(**row(owner=400), operation_key="retry")
            self.assertEqual(await self.dump(), before)
            await self.service.delete_link("#P0", operation_key="last")
        self.assertEqual([r["created_at"] for r in await self.rows(
            "SELECT created_at FROM link_operations ORDER BY created_at")], [300, 301, 302])

    async def test_explicit_ties_refresh_and_advancing_clock_share_commit_time(self):
        before = await self.dump()
        with self.assertRaisesRegex(LinkConflict, "operation_precedes_import"):
            await self.service.delete_link("#P0", at=199)
        self.assertEqual(await self.dump(), before)
        await self.service.upsert_link(**row("#V0", 300), at=300)
        await self.service.update_link_last_seen(player_tag="#V0", player_name_last_seen="Fresh",
            last_seen_clan_tag="", last_seen_clan_code="", last_seen_role="", at=300)
        await self.service.upsert_link(**row("#V0", 400), at=300)
        await self.service.delete_link("#V0", at=400)
        self.assertEqual([r["created_at"] for r in await self.rows(
            "SELECT created_at FROM link_operations ORDER BY created_at")], [300, 301, 302, 400])
        history = await self.rows("SELECT l.from_at,l.last_observed_at,l.to_at,l.end_previous_good_at "
                                  "FROM account_links l JOIN accounts a ON a.id=l.account_id "
                                  "WHERE a.player_tag='#V0' ORDER BY l.from_at")
        self.assertEqual(history, [dict(from_at=300, last_observed_at=301, to_at=302, end_previous_good_at=301),
                                   dict(from_at=302, last_observed_at=302, to_at=400, end_previous_good_at=302)])
        # Ordering comes from the link tables; the import marker is no longer advanced.
        self.assertEqual((await self.rows("SELECT checked_at FROM imports"))[0]["checked_at"], 200)


    async def test_unavailable_record_never_reads_or_writes_legacy(self):
        before = self.legacy.read_bytes()
        with patch.object(self.store, "_connection", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                await self.service.read_links()
            with self.assertRaises(OSError):
                await self.service.upsert_link(**row("#V0", 300))
        self.assertEqual(self.legacy.read_bytes(), before)
        self.assertNotIn("#V0", await self.service.read_links())

    async def test_old_pending_mirror_intent_refuses_access_without_guessing_or_changing_files(self):
        await self.execute("INSERT INTO link_operations(operation_key,requested_json,before_json,after_json,"
                           "before_digest,after_digest,created_at,status) "
                           "VALUES ('old','{}','{}','{}','before','after',250,'pending')")
        before, old = await self.dump(), self.legacy.read_bytes()
        with self.assertRaisesRegex(LinkConflict, 'link_recovery_required'):
            await self.service.read_links()
        with self.assertRaises(LinkConflict):
            await self.service.delete_link('#P0', at=300)
        self.assertEqual(await self.dump(), before)
        self.assertEqual(self.legacy.read_bytes(), old)

    async def test_read_and_write_disk_work_requires_workers(self):
        threads, real = [], self.store._call
        def observe(*args, **kwargs):
            threads.append(threading.get_ident())
            return real(*args, **kwargs)
        with patch.object(self.store, "_call", side_effect=observe):
            await self.service.read_links()
            await self.service.delete_link("#R0", at=300)
        self.assertNotIn(threading.get_ident(), threads)
        with self.assertRaisesRegex(RuntimeError, "link_read_requires_worker"):
            LinkFacade(self.service).all()

    async def test_refresh_only_updates_existing_links_and_preserves_owner(self):
        metadata = dict(player_name_last_seen="Fresh", last_seen_clan_tag="", last_seen_clan_code="", last_seen_role=None)
        await self.service.update_link_last_seen(player_tag="#P0", **metadata, at=300)
        await self.service.update_link_last_seen(player_tag="#V0", **metadata, at=400)
        links = await self.service.read_links()
        self.assertEqual(links["#P0"], {"player_tag": "#P0", "discord_user_id": 100, **metadata})
        self.assertNotIn("#V0", links)
        self.assertEqual(len(await self.rows("SELECT * FROM account_links")), 3)


    async def test_record_rosters_cover_success_empty_failure_staleness_and_generations(self):
        reads = MembershipReads(self.store, clans=CLANS.values())
        snapshot = await reads.rosters(as_of=200)
        self.assertTrue(snapshot["complete"])
        self.assertEqual(sum(len(row["members"]) for row in snapshot["rows"]), 2)
        self.assertEqual(snapshot["rows"][0]["members"][0]["player_name"], "Player")
        self.assertFalse((await reads.rosters(as_of=2_000_000_000))["complete"])
        await self.execute("UPDATE collection_items SET status='failed' WHERE source_kind='clan_roster' AND ordinal=1")
        snapshot = await reads.rosters(as_of=200)
        self.assertFalse(snapshot["complete"])
        self.assertIsNone(snapshot["rows"][0]["members"])


    async def test_sql_reader_uses_record_owners_and_keeps_review_state_separate(self):
        await self.service.upsert_link(**row(owner=300), at=300)
        await self.service.delete_link("#Q0", at=400)
        # Deliberately stale rollback data must never become a read fallback.
        with closing(sqlite3.connect(self.legacy)) as connection, connection:
            connection.execute("UPDATE links SET discord_user_id=999")
        paths = SimpleNamespace(data_root=self.root, project_root=self.root)
        # Reviews and ownership now share the canonical family database.
        await self.execute("INSERT INTO suggestions VALUES ('#G0','Candidate','ONE','#PP',100,NULL,1,2)")
        destination = self.store.path
        sources = (replace(SOURCES[0], path=destination),)
        before = self.legacy.read_bytes()
        result, _ = await asyncio.to_thread(run_query, sources, paths, set(),
            "SELECT player_tag,discord_user_id FROM links.links ORDER BY player_tag")
        self.assertEqual(result['rows'], [{'player_tag': '#P0', 'discord_user_id': 300},
                                          {'player_tag': '#R0', 'discord_user_id': 200}])
        result, _ = await asyncio.to_thread(run_query, sources, paths, {'recruiter_or_core'},
                                           "SELECT player_tag FROM links.suggestions")
        self.assertEqual(result['rows'], [{'player_tag': '#G0'}])
        destination.unlink()
        result, _ = await asyncio.to_thread(run_query, sources, paths, set(), "SELECT * FROM links.links")
        self.assertIn('error', result)
        self.assertEqual(self.legacy.read_bytes(), before)

    async def test_local_ownership_never_ages_out_between_commands(self):
        people = PeopleReads(self.store)
        result = await people.owners(['#P0'], 2_000_000_000)
        self.assertEqual(result['rows'][0]['member_id'], 100)
        self.assertTrue(result['coverage']['complete'])
        await self.service.upsert_link(**row(owner=300), at=300)
        result = await people.owners(['#P0'], 2_000_000_000)
        self.assertEqual(result['rows'][0]['member_id'], 300)
        self.assertTrue(result['coverage']['complete'])

    async def test_cancellation_finishes_the_first_commit_before_the_next_writer(self):
        entered, release = threading.Event(), threading.Event()
        real = self.service._mutate
        def mutate(connection, requested, key, at, kind):
            if at == 300:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test release')
            return real(connection, requested, key, at, kind)
        with patch.object(self.service, '_mutate', side_effect=mutate):
            first = asyncio.create_task(self.service.upsert_link(**row(owner=300), at=300))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(self.service.upsert_link(**row(owner=400), at=400))
            release.set()
            await second
            await self.service.close()
        self.assertEqual((await self.service.read_links())['#P0']['discord_user_id'], 400)
        self.assertEqual(len(await self.rows('SELECT * FROM link_operations')), 2)
