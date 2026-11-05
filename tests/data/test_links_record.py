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
from elbow_helper.data.store.legacy_links import LinkConflict, snapshot
from elbow_helper.features.account_links.cog import AccountLinks
from elbow_helper.features.rosters.services.accounts import RosterAccountDirectory
from elbow_helper.agent.capabilities.account_links.reads import read_linked_accounts
from elbow_helper.agent.capabilities.account_links.data import SOURCES
from elbow_helper.agent.datasets.query import run_query

from .link_fixtures import CLANS, FrozenLegacyLinks
from .test_people_import import _PeopleFixture, ProcessCrash, row


class RecordLinkTests(_PeopleFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.assertTrue((await self.service.startup(at=200))["ready"])
        self.cog = AccountLinks(SimpleNamespace(link_service=self.service), SimpleNamespace(configured=False))
        self.cog._try_refresh_linked_boards = AsyncMock()
        self.cog.refresh_linked_boards = AsyncMock()

    async def test_every_reader_preserves_rows_order_nulls_and_empty_members(self):
        old = FrozenLegacyLinks(self.legacy)
        consumers = (
            ("account commands and reviews", "get_link_by_tag", ("#P0",)),
            ("recruitment", "get_links_by_tags", (["#Q0", "#P0", "#V0", "#Q0"],)),
            ("rosters", "get_links_for_user", (100,)),
            ("CWL rosters and transfers", "get_links_for_user", (200,)),
            ("missing Elder boards", "get_all_links", ()),
            ("war roles", "get_all_links", ()),
            ("ranked population", "get_all_links", ()),
            ("agent identities, cohorts and workflow reads", "get_links_for_members", ([200, 100, 999],)),
            ("agent names, wars and projections", "get_links_by_tags", (["#Q0", "#P0", "#V0"],)),
        )
        for consumer, method, args in consumers:
            with self.subTest(consumer=consumer):
                before = await asyncio.to_thread(getattr(old, method), *args)
                after = await asyncio.to_thread(getattr(self.cog, method), *args)
                self.assertEqual(after, before)
                if isinstance(before, dict):
                    self.assertEqual(tuple(after), tuple(before))
        old.get_player_location = self.cog.get_player_location
        self.assertEqual(await asyncio.to_thread(RosterAccountDirectory(old).for_member, 100),
                         await asyncio.to_thread(RosterAccountDirectory(self.cog).for_member, 100))
        with patch("elbow_helper.agent.capabilities.account_links.reads.require_evidence_access", AsyncMock()):
            before = await read_linked_accounts(SimpleNamespace(account_links=old), {"member_ids": [100, 200, 999]})
            after = await read_linked_accounts(SimpleNamespace(account_links=self.cog), {"member_ids": [100, 200, 999]})
        self.assertEqual(after, before)

    async def assert_rollback_copy(self):
        old = await asyncio.to_thread(snapshot, self.legacy)
        self.assertEqual(old.links, await self.service.read_links())
        self.assertEqual(tuple(old.links), tuple(await self.service.read_links()))
        return old

    async def test_add_transfer_delete_refresh_and_undo_keep_rollback_copy_current(self):
        original = await self.assert_rollback_copy()
        before = await self.service.read_links()
        await self.service.upsert_link(**row("#V0", 300, "Alt"), at=250)
        await self.assert_rollback_copy()
        await self.service.upsert_link(**row(owner=300, name="New"), at=300)
        await self.assert_rollback_copy()
        await self.service.delete_link("#Q0", at=400)
        await self.assert_rollback_copy()
        await self.service.update_link_last_seen(player_tag="#P0", player_name_last_seen="Fresh",
            last_seen_clan_tag=None, last_seen_clan_code="", last_seen_role=None, at=450)
        await self.assert_rollback_copy()
        await self.service.restore_links({**before, "#V0": None}, at=500)
        self.assertEqual(await self.service.read_links(), before)
        final = await self.assert_rollback_copy()
        self.assertEqual(final.auxiliary, original.auxiliary)
        self.assertEqual(final.version, original.version)
        self.assertEqual([r["status"] for r in await self.rows("SELECT status FROM link_operations")], ["complete"] * 5)
        history = await PeopleReads(self.store).account_history(["#P0"], 600)
        self.assertEqual([r["member_id"] for r in history["rows"][0]["record_history"]["ownership_intervals"]],
                         [100, 300, 100])

    async def test_each_late_batch_failure_leaves_no_identity_history_receipt_or_metadata(self):
        for target, event in (("members", "INSERT"), ("account_links", "INSERT"),
                              ("account_link_metadata", "INSERT"), ("link_operations", "INSERT"),
                              ("imports", "UPDATE")):
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
        restart = LinkService(FamilyStore(self.store.path), self.legacy)
        with patch("elbow_helper.data.links.import_people", side_effect=AssertionError("old links read")):
            self.assertTrue((await restart.startup())["ready"])
            with self.assertLogs("elbow_helper.data.links", level="WARNING") as captured:
                await restart.upsert_link(**row("#V0", 300), at=300)
        self.assertEqual(len(captured.output), 1)
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

    async def test_rollback_copy_runs_after_commit_in_worker_without_reading_old_rows(self):
        real_connect = sqlite3.connect
        threads, statements = [], []
        def connect(path, *args, **kwargs):
            connection = real_connect(path, *args, **kwargs)
            if str(path) == self.legacy.resolve().as_uri() + "?mode=rw":
                threads.append(threading.get_ident())
                with closing(real_connect(self.store.path)) as reader:
                    self.assertEqual(reader.execute(
                        "SELECT discord_user_id FROM current_link_rows WHERE player_tag='#V0'").fetchone()[0], 300)
                def authorize(action, table, column, database, source):
                    if action == sqlite3.SQLITE_READ and table == "links":
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                connection.set_authorizer(authorize)
                connection.set_trace_callback(statements.append)
            return connection
        with patch("elbow_helper.data.links.sqlite3.connect", side_effect=connect):
            await self.service.upsert_link(**row("#V0", 300), at=300)
        self.assertEqual(len(threads), 1)
        self.assertNotIn(threading.get_ident(), threads)
        self.assertTrue(any(sql.startswith("INSERT INTO links") for sql in statements))
        await self.assert_rollback_copy()

    async def test_failed_copy_rolls_back_old_batch_and_next_change_repairs_it(self):
        def trigger(sql):
            with closing(sqlite3.connect(self.legacy)) as connection, connection:
                connection.execute(sql)
        await asyncio.to_thread(trigger, "CREATE TRIGGER refuse_copy BEFORE INSERT ON links "
            "WHEN NEW.player_tag='#V0' BEGIN SELECT RAISE(ABORT,'synthetic old write failure'); END")
        original = self.legacy.read_bytes()
        with self.assertLogs("elbow_helper.data.links", level="WARNING") as captured:
            result = await self.service.upsert_links([row(owner=300), row("#V0", 300)],
                                                   operation_key="failed-copy", at=300)
        self.assertEqual(result, "failed-copy")
        self.assertEqual(len(captured.output), 1)
        self.assertIn("upsert operation failed-copy", captured.output[0])
        self.assertEqual(self.legacy.read_bytes(), original)
        self.assertEqual((await self.service.read_links())["#P0"]["discord_user_id"], 300)
        self.assertEqual((await self.service.read_links())["#V0"]["discord_user_id"], 300)
        self.assertEqual((await self.rows("SELECT status FROM link_operations"))[0]["status"], "complete")
        await asyncio.to_thread(trigger, "DROP TRIGGER refuse_copy")
        await self.service.delete_link("#Q0", at=400)
        await self.assert_rollback_copy()

    async def test_locked_old_file_does_not_block_a_record_commit(self):
        with closing(sqlite3.connect(self.legacy)) as locked:
            locked.execute("BEGIN IMMEDIATE")
            with self.assertLogs("elbow_helper.data.links", level="WARNING") as captured:
                await asyncio.wait_for(self.service.delete_link("#P0", operation_key="locked", at=300), 2)
            self.assertNotIn("#P0", await self.service.read_links())
            self.assertEqual(len(captured.output), 1)
            self.assertIn("delete operation locked", captured.output[0])
            locked.rollback()

    async def test_failed_old_write_keeps_link_transfer_and_unlink_replies(self):
        member = SimpleNamespace(id=300, mention="<@300>")
        self.cog.lookup_players = AsyncMock(return_value=[{"player_tag": "#P0Y", "player_name": "Alpha"}])
        for kind in ("link", "transfer", "unlink"):
            with self.subTest(kind=kind):
                await self.service.restore_links({"#P0Y": None if kind == "link" else row("#P0Y", 100, "Alpha")})
                interaction = SimpleNamespace(user=SimpleNamespace(),
                    response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
                operation = self.cog.account_remove_operation if kind == "unlink" else self.cog.account_add_operation
                args = ("#P0Y",) if kind == "unlink" else (member, "#P0Y")
                expected = await operation(*args, commit=False)
                with patch.object(self.cog, "_can_manage_accounts", return_value=True), self.assertLogs(
                        "elbow_helper.data.links", level="WARNING") as captured:
                    with closing(sqlite3.connect(self.legacy)) as locked:
                        locked.execute("BEGIN IMMEDIATE")
                        if kind == "unlink":
                            await AccountLinks.account_remove.callback(self.cog, interaction, "#P0Y")
                        else:
                            await AccountLinks.account_add.callback(self.cog, interaction, member, "#P0Y")
                        locked.rollback()
                interaction.followup.send.assert_awaited_once_with(expected["message"], ephemeral=True)
                self.assertEqual(len(captured.output), 1)
                current = await self.service.read_links()
                if kind == "unlink":
                    self.assertNotIn("#P0Y", current)
                else:
                    self.assertEqual(current["#P0Y"]["discord_user_id"], 300)

    async def test_refresh_only_updates_existing_links_and_preserves_owner(self):
        metadata = dict(player_name_last_seen="Fresh", last_seen_clan_tag="", last_seen_clan_code="", last_seen_role=None)
        await self.service.update_link_last_seen(player_tag="#P0", **metadata, at=300)
        await self.service.update_link_last_seen(player_tag="#V0", **metadata, at=400)
        links = await self.service.read_links()
        self.assertEqual(links["#P0"], {"player_tag": "#P0", "discord_user_id": 100, **metadata})
        self.assertNotIn("#V0", links)
        self.assertEqual(len(await self.rows("SELECT * FROM account_links")), 3)

    async def test_sqlite_sorting_and_undo_natural_order_match_original(self):
        rows = {"#P0": row(name="é"), "#Q0": row("#Q0", name="Z"), "#R0": row("#R0", 100, "a\0suffix"),
                "#V0": row("#V0", 100, "A"), "#G0": row("#G0", 100, ""), "#C0": row("#C0", 100, None)}
        await self.service.restore_links(rows, at=300)
        await self.assert_rollback_copy()
        self.assertEqual(await asyncio.to_thread(self.cog.get_links_for_user, 100),
                         await asyncio.to_thread(FrozenLegacyLinks(self.legacy).get_links_for_user, 100))
        await self.service.restore_links({tag: rows[tag] for tag in ("#Q0", "#P0")}, at=400)
        self.assertEqual(tuple(await self.service.read_links()), ("#R0", "#V0", "#G0", "#C0", "#Q0", "#P0"))

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

    async def test_roster_member_without_name_or_role_reads_as_the_old_reader_did(self):
        # The old reader kept any member with a tag, naming it by its tag and leaving the role empty.
        stored = {"#P0": {"name": "Player", "role": "member", "townHallLevel": 16}, "#X0": {"townHallLevel": 12}}
        reads = MembershipReads(self.store, clans=CLANS.values())
        with patch("elbow_helper.data.reads.membership.roster_fields", return_value=stored):
            snapshot = await reads.rosters(as_of=200)
        self.assertTrue(snapshot["complete"])
        members = snapshot["rows"][0]["members"]
        self.assertEqual(members, [
            {"player_tag": "#P0", "player_name": "Player", "townhall": 16, "role": "member"},
            {"player_tag": "#X0", "player_name": "#X0", "townhall": 12, "role": ""}])
        with patch("elbow_helper.data.reads.membership.roster_fields", return_value={"#X0": {}}):
            members = (await reads.rosters(as_of=200))["rows"][0]["members"]
        self.assertEqual(members, [{"player_tag": "#X0", "player_name": "#X0", "townhall": 0, "role": ""}])

    async def test_sql_reader_uses_record_owners_and_keeps_review_state_separate(self):
        await self.service.upsert_link(**row(owner=300), at=300)
        await self.service.delete_link("#Q0", at=400)
        # Deliberately stale rollback data must never become a read fallback.
        with closing(sqlite3.connect(self.legacy)) as connection, connection:
            connection.execute("UPDATE links SET discord_user_id=999")
        paths = SimpleNamespace(data_root=self.root, project_root=self.root)
        destination = self.root / 'family' / 'family.sqlite3'
        destination.parent.mkdir()
        def copy(connection):
            with closing(sqlite3.connect(destination)) as target:
                connection.backup(target)
        await self.store.call(copy)
        sources = (replace(SOURCES[0], path=self.legacy),)
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
        await self.assert_rollback_copy()
