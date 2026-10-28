"""Offline import rehearsal, refusal cases, and three-commit crash recovery."""

import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from .link_fixtures import CLANS
from elbow_helper.data.links import LinkService
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.clans import collect_clans
from elbow_helper.data.store.legacy_links import (
    BootstrapRequired, FIELDS, ImportMismatch, LinkConflict, legacy_shape, snapshot,
)
from elbow_helper.data.store.people import mirror_links
from elbow_helper.data.store import people_import
from elbow_helper.data.store.api_fields import _pack, unpack
from elbow_helper.data.store.schema import MIGRATIONS
from elbow_helper.infrastructure.persistence.migrations import run_sqlite_migrations

from .test_family_store import player, response


def row(tag="#P0", owner=100, name=None):
    return dict(zip(FIELDS, (tag, owner, name, "#QQ", None, "")))


class ProcessCrash(BaseException):
    """The process stops before it can run in-process recovery."""


class _PeopleFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clan_patch = patch.object(people_import, "CLANS", CLANS)
        clan_patch.start()
        self.addCleanup(clan_patch.stop)
        self.temporary = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.legacy = self.root / "links.sqlite3"
        with closing(sqlite3.connect(self.legacy)) as c, c:
            legacy_shape(c)
            c.execute("PRAGMA user_version=1")
            c.executemany("INSERT INTO links VALUES (?,?,?,?,?,?)",
                          [tuple(row().values()), tuple(row("#Q0", name="").values()), tuple(row("#R0", 200, "Outside").values())])
            c.execute("INSERT INTO ignored_tags VALUES ('#C0')")
            c.execute("INSERT INTO suggestions VALUES ('#G0','Candidate','ONE','#PP',100,NULL,1,2)")
        self.store = FamilyStore(self.root / "family.sqlite3")
        await self.store.initialize()
        self.service = LinkService(self.store, self.legacy)
        await self.clans()

    async def clans(self, at=100):
        clans = tuple(CLANS.values())
        specs = [(kind, clan.tag) for clan in clans for kind in ("clan_profile", "clan_roster")]
        run = await self.store.begin_run("clans", "family_clans", specs, at)
        responses = [response({"tag": clan.tag, "name": clan.name,
                     "members": 1 if i < 2 else 0,
                     "memberList": [player("#P0" if i == 0 else "#Q0")] if i < 2 else []})
                     for i, clan in enumerate(clans)]
        await self.store.call(collect_clans, run, clans, responses, at)
        await self.store.finish(run)

    async def rows(self, sql, args=()):
        return await self.store.call(lambda c: [dict(r) for r in c.execute(sql, args)])

    async def execute(self, sql, args=()):
        def run(c):
            with c:
                c.execute(sql, args)
        await self.store.call(run)

    async def dump(self):
        return await self.store.call(lambda c: tuple(c.iterdump()))

    async def shadow_links(self, rows=None, at=150):
        run = await self.store.begin_run("links", "current", [("account_links", "current")], at)
        await self.store.call(mirror_links, run, snapshot(self.legacy).links if rows is None else rows, at)
        await self.store.finish(run)

    async def assert_import_rollback(self, error=ImportMismatch):
        before = await self.dump()
        original = self.legacy.read_bytes()
        with self.assertRaises(error):
            await self.store.call(people_import.import_people, self.legacy, snapshot(self.legacy, 200))
        self.assertEqual(await self.dump(), before)
        self.assertEqual(self.legacy.read_bytes(), original)
        self.assertEqual(await self.rows("SELECT * FROM imports"), [])


class PeopleImportTests(_PeopleFixture):
    async def test_exact_import_preserves_null_empty_outside_links_and_observed_history(self):
        await self.shadow_links()
        observed = await self.rows("SELECT * FROM account_links ORDER BY id")
        original = self.legacy.read_bytes()
        report = await self.service.startup(at=200)
        checks = report["checks"]
        self.assertEqual((checks["links"], checks["members"], checks["outside_family_links"]), (3, 2, 1))
        self.assertEqual(checks["per_member_counts"], {"100": 2, "200": 1})
        self.assertEqual((checks["owner_pairs"], checks["compatibility_fields"], checks["roster_accounts"]), (3, 18, 2))
        self.assertEqual((checks["suggestions"], checks["ignored_tags"], len(checks["clans"])), (1, 1, 9))
        self.assertEqual(self.legacy.read_bytes(), original)
        frozen = snapshot(self.legacy)
        self.assertEqual({r["player_tag"]: r for r in await self.rows("SELECT * FROM legacy_links")}, frozen.normalized)
        links = await self.rows("SELECT * FROM account_links ORDER BY id")
        self.assertEqual([(r["from_at"], r["to_at"], r["basis"]) for r in links],
                         [(150, None, "legacy_current")] * 3)
        item = (await self.rows("SELECT source_item_id FROM imports"))[0]["source_item_id"]
        self.assertEqual(links, [{**r, "last_observed_at": 200, "basis": "legacy_current", "source_item_id": item}
                                 for r in observed])
        metadata = await self.rows("SELECT * FROM account_link_metadata")
        self.assertTrue(all(r["observed_at"] is None and r["basis"] == "legacy_unknown_age" for r in metadata))
        self.assertEqual(len(await self.rows("SELECT * FROM clan_membership")), 2)
        self.assertEqual((await self.rows("SELECT current_name FROM accounts WHERE player_tag='#P0'"))[0]["current_name"], "Player")
        aliases = await self.rows("SELECT * FROM legacy_link_aliases")
        self.assertEqual({r["name"] for r in aliases}, {"", "Outside"})
        self.assertTrue(all(r["observed_at"] is None for r in aliases))

    async def test_import_keeps_matching_owner_changes_different_owner_and_adds_missing_link(self):
        observed = {r["player_tag"]: r for r in (row(), row("#Q0", 300), row("#V0", 400))}
        await self.shadow_links(observed)
        report = await self.service.startup(at=200)
        self.assertTrue(report["mirrored"])
        self.assertEqual(report["checks"]["links"], 3)
        links = await self.rows("SELECT a.player_tag,m.discord_id,l.from_at,l.to_at,l.basis,l.end_previous_good_at "
                                "FROM account_links l JOIN accounts a ON a.id=l.account_id "
                                "JOIN members m ON m.id=l.member_id ORDER BY a.player_tag,l.from_at")
        self.assertEqual([(r["player_tag"], r["discord_id"], r["from_at"], r["to_at"], r["basis"],
                           r["end_previous_good_at"]) for r in links],
                         [("#P0", 100, 150, None, "legacy_current", None),
                          ("#Q0", 300, 150, 200, "shadow_observed", 150),
                          ("#Q0", 100, 200, None, "legacy_current", None),
                          ("#R0", 200, 200, None, "legacy_current", None),
                          ("#V0", 400, 150, 200, "shadow_observed", 150)])
        before = await self.dump()
        self.assertFalse((await self.service.startup(at=201))["imported"])
        self.assertEqual(await self.dump(), before)

    async def test_repeat_startup_is_a_noop_and_checks_current_legacy(self):
        await self.service.startup(at=200)
        before = await self.dump()
        self.service = LinkService(FamilyStore(self.store.path), self.legacy)
        report = await self.service.startup(at=201)
        self.assertFalse(report["imported"])
        self.assertEqual(await self.dump(), before)

    async def test_unknown_age_legacy_alias_never_replaces_a_fresh_observed_name(self):
        with closing(sqlite3.connect(self.legacy)) as c, c:
            c.execute("UPDATE links SET player_name_last_seen='Older name' WHERE player_tag='#P0'")
        await self.service.startup(at=200)
        names = await self.rows("SELECT a.current_name,n.name,n.from_at FROM accounts a "
                                "JOIN account_aliases n ON n.account_id=a.id WHERE a.player_tag='#P0' AND n.to_at IS NULL")
        self.assertEqual(names, [{"current_name": "Player", "name": "Player", "from_at": 100}])
        imported = await self.rows("SELECT n.name,n.observed_at,n.imported_at,n.basis FROM legacy_link_aliases n "
                                   "JOIN accounts a ON a.id=n.account_id WHERE a.player_tag='#P0'")
        self.assertEqual(imported, [{"name": "Older name", "observed_at": None, "imported_at": 200, "basis": "legacy_unknown_age"}])

    async def test_known_unversioned_source_is_read_only(self):
        with closing(sqlite3.connect(self.legacy)) as c:
            c.execute("PRAGMA user_version=0")
        original = self.legacy.read_bytes()
        report = await self.service.startup(at=200)
        self.assertEqual(report["source_version"], 0)
        self.assertEqual(self.legacy.read_bytes(), original)

    async def test_normalization_collision_invalid_owner_tag_and_metadata_roll_back(self):
        cases = ["UPDATE links SET player_tag='INVALID' WHERE player_tag='#P0'",
                 "INSERT INTO links VALUES ('p0',300,'','','','')",
                 "UPDATE links SET discord_user_id=0 WHERE player_tag='#P0'",
                 "UPDATE links SET last_seen_role=x'00' WHERE player_tag='#P0'"]
        for sql in cases:
            with self.subTest(sql=sql):
                with closing(sqlite3.connect(self.legacy)) as c:
                    c.execute("BEGIN")
                    c.execute(sql)
                    c.commit()
                await self.assert_import_rollback()
                with closing(sqlite3.connect(self.legacy)) as c, c:
                    c.execute("DELETE FROM links")
                    c.executemany("INSERT INTO links VALUES (?,?,?,?,?,?)", [tuple(row().values()),
                                      tuple(row("#Q0", name="").values()), tuple(row("#R0", 200, "Outside").values())])

    async def test_noncanonical_tags_import_and_mutations_preserve_legacy_spelling(self):
        with closing(sqlite3.connect(self.legacy)) as c, c:
            c.execute("UPDATE links SET player_tag='pO' WHERE player_tag='#P0'")
        await self.service.startup(at=200)
        await self.service.upsert_link(player_tag="#P0", discord_user_id=300, at=300)
        old = await self.service.read_links()
        self.assertIn("pO", old)
        self.assertEqual(old["pO"]["discord_user_id"], 300)
        self.assertEqual((await self.rows("SELECT discord_user_id FROM legacy_links WHERE player_tag='#P0'"))[0]["discord_user_id"], 300)

    async def test_missing_and_unsupported_or_malformed_sources_refuse_without_creating_files(self):
        self.service.legacy_path = self.root / "absent.sqlite3"
        with self.assertLogs("elbow_helper.data.links", level="WARNING"):
            report = await self.service.startup(at=200)
        self.assertFalse(report["mirrored"])
        self.assertFalse(self.service.legacy_path.exists())
        self.service.legacy_path = self.legacy
        with closing(sqlite3.connect(self.legacy)) as c:
            c.execute("PRAGMA user_version=99")
        await self.assert_import_rollback()
        with closing(sqlite3.connect(self.legacy)) as c, c:
            c.execute("PRAGMA user_version=1")
            c.execute("DROP TABLE suggestions")
        await self.assert_import_rollback(RuntimeError)

    async def test_changed_legacy_schema_and_unique_keys_are_rejected(self):
        with closing(sqlite3.connect(self.legacy)) as c, c:
            c.execute("ALTER TABLE suggestions ADD COLUMN unexpected TEXT")
        await self.assert_import_rollback()
        with closing(sqlite3.connect(self.legacy)) as c, c:
            c.execute("ALTER TABLE suggestions DROP COLUMN unexpected")
            c.execute("CREATE TABLE unkeyed AS SELECT * FROM ignored_tags")
            c.execute("DROP TABLE ignored_tags")
            c.execute("ALTER TABLE unkeyed RENAME TO ignored_tags")
        await self.assert_import_rollback(RuntimeError)

    async def test_missing_stale_partial_and_inconsistent_generations_roll_back(self):
        cases = ["UPDATE collection_items SET source_kind='other' WHERE source_kind='clan_roster'",
                 "UPDATE collection_items SET fetched_at=-2000000000 WHERE source_kind='clan_roster'",
                 "UPDATE collection_items SET status='failed' WHERE source_kind='clan_roster'",
                 "UPDATE collection_items SET completeness_json='{}' WHERE source_kind='clan_roster'",
                 "UPDATE collection_items SET run_id=(SELECT MAX(id) FROM collection_runs) WHERE ordinal=1"]
        # Separate valid run permits a synthetic mixed generation without FK violations.
        await self.store.begin_run("other", "other", [], 101)
        for sql in cases:
            with self.subTest(sql=sql):
                def try_case(c):
                    c.execute("BEGIN")
                    c.execute(sql)
                    # Commit the corruption as the pre-import state.
                    c.commit()
                before = await self.rows("SELECT * FROM collection_items ORDER BY id")
                await self.store.call(try_case)
                await self.assert_import_rollback(BootstrapRequired)
                def restore(c):
                    with c:
                        c.execute("DELETE FROM collection_items")
                        if before:
                            columns = tuple(before[0])
                            c.executemany(f"INSERT INTO collection_items({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                                          [tuple(r.values()) for r in before])
                # Existing clan facts reference those items, so defer their checks.
                def restore_deferred(c):
                    c.execute("PRAGMA foreign_keys=OFF")
                    try:
                        restore(c)
                    finally:
                        c.execute("PRAGMA foreign_keys=ON")
                await self.store.call(restore_deferred)

    async def test_roster_count_tag_role_and_profile_mismatches_roll_back(self):
        cases = ["UPDATE collection_items SET returned_count=5 WHERE source_kind='clan_roster' AND ordinal=1",
                 "UPDATE clan_membership SET clan_role='admin' WHERE id=1",
                 "UPDATE accounts SET player_tag='#V0' WHERE player_tag='#P0'",
                 "UPDATE clans SET is_family=0 WHERE id=1"]
        for sql in cases:
            with self.subTest(sql=sql):
                # Patch the validation input after the real importer wrote its rows.
                real_apply = people_import.apply_rows
                def corrupt(c, *args, **kwargs):
                    real_apply(c, *args, **kwargs)
                    c.execute(sql)
                with patch.object(people_import, "apply_rows", corrupt):
                    await self.assert_import_rollback()
        await self.execute("UPDATE api_records SET state_z=? WHERE scope_id=?", (b"invalid", next(iter(CLANS.values())).tag))
        await self.assert_import_rollback(Exception)

    async def test_missing_th_profile_and_duplicate_locations_roll_back(self):
        def corrupt(c):
            record = c.execute("SELECT * FROM api_records WHERE scope_id=?", (next(iter(CLANS.values())).tag,)).fetchone()
            paths, state = unpack(record["state_z"])
            for slot in list(state):
                if paths[int(slot)].endswith("/townHallLevel"):
                    del state[slot]
            with c:
                c.execute("UPDATE api_records SET state_z=? WHERE id=?", (_pack([paths, state]), record["id"]))
        await self.store.call(corrupt)
        await self.assert_import_rollback(BootstrapRequired)
        await self.clans(101)
        await self.execute("UPDATE collection_items SET source_kind='other' WHERE source_kind='clan_profile'")
        await self.assert_import_rollback(BootstrapRequired)
        await self.execute("UPDATE collection_items SET source_kind='clan_profile' WHERE source_kind='other'")
        await self.execute("DROP INDEX clan_open")
        await self.execute("INSERT INTO clan_membership(account_id,clan_id,clan_role,from_at,last_observed_at,source_item_id) "
                           "SELECT account_id,2,clan_role,150,last_observed_at,source_item_id FROM clan_membership WHERE id=1")
        await self.assert_import_rollback()

    async def test_initial_import_failure_at_commit_rolls_back_marker_and_all_facts(self):
        await self.shadow_links()
        await self.execute("CREATE TRIGGER refuse_import BEFORE INSERT ON imports "
                           "BEGIN SELECT RAISE(ABORT,'synthetic commit boundary'); END")
        await self.assert_import_rollback(sqlite3.IntegrityError)

    async def test_freeze_before_shadow_observation_rolls_back(self):
        await self.shadow_links(at=300)
        await self.assert_import_rollback()

    async def test_version_seven_upgrade_preserves_existing_people(self):
        old = self.root / "version-seven.sqlite3"
        with closing(sqlite3.connect(old)) as c:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            run_sqlite_migrations(c, tuple(m for m in MIGRATIONS if m.version <= 7), target_version=7)
            with c:
                c.execute("INSERT INTO accounts(player_tag,first_observed_at,tracking_status) VALUES ('#V0',50,'observed')")
        upgraded = FamilyStore(old)
        await upgraded.initialize()
        saved = await upgraded.call(lambda c: dict(c.execute("SELECT * FROM accounts").fetchone()))
        self.assertEqual((saved["player_tag"], saved["first_observed_at"]), ("#V0", 50))
        self.assertEqual(await upgraded.call(lambda c: c.execute("SELECT COUNT(*) FROM imports").fetchone()[0]), 0)

    async def test_mapping_tag_owner_metadata_and_foreign_key_mismatches_roll_back(self):
        cases = ["DELETE FROM account_links WHERE account_id=(SELECT id FROM accounts WHERE player_tag='#P0')",
                 "UPDATE account_links SET member_id=(SELECT id FROM members WHERE discord_id=200) WHERE id=1",
                 "UPDATE account_link_metadata SET last_seen_role=NULL WHERE account_id=1",
                 "DELETE FROM account_link_metadata WHERE account_id=1"]
        for sql in cases:
            with self.subTest(sql=sql):
                real_apply = people_import.apply_rows
                def corrupt(c, *args, **kwargs):
                    real_apply(c, *args, **kwargs)
                    c.execute(sql)
                with patch.object(people_import, "apply_rows", corrupt):
                    await self.assert_import_rollback()
        def orphan(c):
            c.execute("PRAGMA foreign_keys=OFF")
            with c:
                c.execute("INSERT INTO account_link_metadata(account_id,basis,source_item_id) VALUES (999,'bad',999)")
            c.execute("PRAGMA foreign_keys=ON")
        await self.store.call(orphan)
        await self.assert_import_rollback()

    async def test_source_changes_during_import_roll_back(self):
        await self.shadow_links()
        real_snapshot = people_import.snapshot
        def changed(*args, **kwargs):
            with closing(sqlite3.connect(self.legacy)) as c, c:
                c.execute("INSERT INTO ignored_tags VALUES ('#Y0')")
            return real_snapshot(*args, **kwargs)
        before = await self.dump()
        with patch.object(people_import, "snapshot", changed):
            with self.assertRaisesRegex(ImportMismatch, "legacy_changed_during_import"):
                await self.store.call(people_import.import_people, self.legacy, snapshot(self.legacy, 200))
        self.assertEqual(await self.dump(), before)

    async def test_snapshot_is_consistent_while_legacy_writer_commits(self):
        from elbow_helper.data.store import legacy_links
        with closing(sqlite3.connect(self.legacy)) as c:
            c.execute("PRAGMA journal_mode=WAL")
        real_rows = legacy_links.link_rows
        def concurrent(c):
            rows = real_rows(c)
            with closing(sqlite3.connect(self.legacy)) as writer, writer:
                writer.execute("DELETE FROM links WHERE player_tag='#R0'")
                writer.execute("INSERT INTO ignored_tags VALUES ('#Y0')")
            return rows
        with patch.object(legacy_links, "link_rows", concurrent):
            frozen = snapshot(self.legacy, 200)
        self.assertEqual((len(frozen.links), len(frozen.auxiliary["ignored_tags"])), (3, 1))
        later = snapshot(self.legacy)
        self.assertEqual((len(later.links), len(later.auxiliary["ignored_tags"])), (2, 2))
        self.assertNotEqual(frozen.content_digest, later.content_digest)

    async def test_missing_generation_defers_import_without_disabling_legacy(self):
        await self.execute("UPDATE collection_items SET status='failed' WHERE source_kind='clan_roster'")
        with self.assertLogs("elbow_helper.data.links", level="WARNING"):
            result = await self.service.startup(at=200)
        self.assertFalse(result["mirrored"])
        self.assertEqual(len(await self.service.read_links()), 3)
        await self.clans(199)
        self.assertTrue((await self.service.startup(at=200))["mirrored"])


class LinkServiceTests(_PeopleFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.service.startup(at=200)

    async def test_batch_reassign_delete_add_undo_and_same_owner_metadata(self):
        before = await self.service.read_links()
        await self.service.upsert_links([row(owner=300, name="New"), row("#V0", 300, "Alt")], at=300)
        await self.service.delete_link("#Q0", at=400)
        count = len(await self.rows("SELECT * FROM account_links"))
        await self.service.update_link_last_seen(player_tag="#P0", player_name_last_seen="Fresh",
                  last_seen_clan_tag="", last_seen_clan_code=None, last_seen_role="admin", at=500)
        self.assertEqual(len(await self.rows("SELECT * FROM account_links")), count)
        await self.service.restore_links({**before, "#V0": None}, at=600)
        self.assertEqual(await self.service.read_links(), before)
        self.assertEqual((await self.rows("PRAGMA user_version"))[0]["user_version"], 8)
        self.assertEqual(snapshot(self.legacy).version, 1)
        self.assertEqual(len(snapshot(self.legacy).auxiliary["suggestions"]), 1)
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])
        operations = await self.rows("SELECT * FROM link_operations")
        self.assertTrue(all(r["status"] == "complete" for r in operations))

    async def test_crash_before_intent_has_no_effect(self):
        before = await self.dump()
        with patch("elbow_helper.data.links.requested_patch", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                await self.service.upsert_link(**row(owner=300), at=300)
        self.assertEqual(await self.dump(), before)

    async def test_intent_transaction_failure_keeps_record_unchanged_and_writes_legacy(self):
        await self.execute("CREATE TRIGGER fail_intent AFTER INSERT ON link_operations "
                           "BEGIN SELECT RAISE(ABORT,'synthetic intent failure'); END")
        before = await self.dump()
        with self.assertLogs("elbow_helper.data.links", level="WARNING"):
            await self.service.upsert_link(**row(owner=300), at=300)
        self.assertEqual(await self.dump(), before)
        self.assertEqual((await self.service.read_links())["#P0"]["discord_user_id"], 300)
        self.assertFalse(self.service._started)

    async def test_crash_after_intent_aborts_before_image_on_restart(self):
        with patch.object(self.service, "_apply_legacy", side_effect=ProcessCrash()):
            with self.assertRaises(ProcessCrash):
                await self.service.upsert_link(**row(owner=300), operation_key="intent", at=300)
        self.assertEqual((await self.service.read_links())["#P0"]["discord_user_id"], 100)
        self.service = LinkService(FamilyStore(self.store.path), self.legacy)
        await self.service.startup(at=400)
        self.assertEqual((await self.rows("SELECT status FROM link_operations"))[0]["status"], "aborted")
        self.assertEqual((await self.service.read_links())["#P0"]["discord_user_id"], 100)

    async def test_legacy_batch_failure_rolls_back_every_row_then_aborts_intent(self):
        with closing(sqlite3.connect(self.legacy)) as c, c:
            c.execute("CREATE TRIGGER fail_second BEFORE UPDATE ON links WHEN NEW.player_tag='#Q0' "
                      "BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        before = snapshot(self.legacy).links
        with self.assertRaises(sqlite3.IntegrityError):
            await self.service.upsert_links([row(owner=300), row("#Q0", 300)], at=300)
        self.assertEqual(snapshot(self.legacy).links, before)
        await self.service.recover()
        self.assertEqual(await self.service.read_links(), before)

    async def test_crash_after_legacy_commit_finishes_exactly_once_on_restart(self):
        with patch.object(self.service, "_finish", side_effect=ProcessCrash()):
            with self.assertRaises(ProcessCrash):
                await self.service.upsert_link(**row(owner=300), operation_key="legacy", at=300)
        self.assertEqual(snapshot(self.legacy).links["#P0"]["discord_user_id"], 300)
        self.assertEqual((await self.rows("SELECT discord_user_id FROM legacy_links WHERE player_tag='#P0'"))[0]["discord_user_id"], 100)
        self.service = LinkService(FamilyStore(self.store.path), self.legacy)
        report = await self.service.startup(at=400)
        self.assertFalse(report["imported"])
        count = len(await self.rows("SELECT * FROM account_links"))
        await self.service.recover()
        await self.service.upsert_link(**row(owner=300), operation_key="legacy", at=500)
        self.assertEqual(len(await self.rows("SELECT * FROM account_links")), count)
        self.assertEqual((await self.service.read_links())["#P0"]["discord_user_id"], 300)

    async def test_family_transaction_failure_retains_intent_and_rolls_back_facts(self):
        await self.execute("CREATE TRIGGER fail_finish BEFORE UPDATE ON link_operations WHEN NEW.status='complete' "
                           "BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        count = len(await self.rows("SELECT * FROM account_links"))
        with self.assertRaises(sqlite3.IntegrityError):
            await self.service.upsert_link(**row(owner=300), at=300)
        self.assertEqual(len(await self.rows("SELECT * FROM account_links")), count)
        self.assertEqual((await self.rows("SELECT status FROM link_operations"))[0]["status"], "pending")
        await self.execute("DROP TRIGGER fail_finish")
        await self.service.recover()
        self.assertEqual((await self.service.read_links())["#P0"]["discord_user_id"], 300)

    async def test_crash_after_family_commit_can_retry_key_without_reapplying(self):
        real = self.service._finish
        def committed(*args):
            real(*args)
            raise OSError("crash after commit")
        with patch.object(self.service, "_finish", committed):
            with self.assertRaises(OSError):
                await self.service.delete_link("#P0", operation_key="done", at=300)
        before = await self.dump()
        await self.service.delete_link("#P0", operation_key="done", at=400)
        self.assertEqual(await self.dump(), before)

    async def test_mixed_unexpected_and_unrelated_rollback_changes_halt_without_guessing(self):
        with patch.object(self.service, "_finish", side_effect=ProcessCrash()):
            with self.assertRaises(ProcessCrash):
                await self.service.upsert_links([row(owner=300), row("#Q0", 300)], at=300)
        with closing(sqlite3.connect(self.legacy)) as c, c:
            c.execute("UPDATE links SET discord_user_id=100 WHERE player_tag='#Q0'")
        before = await self.dump()
        with self.assertRaisesRegex(LinkConflict, "image_conflict"):
            await self.service.recover()
        self.assertEqual(await self.dump(), before)
        with closing(sqlite3.connect(self.legacy)) as c, c:
            c.execute("UPDATE links SET discord_user_id=300 WHERE player_tag='#Q0'")
            c.execute("UPDATE links SET discord_user_id=400 WHERE player_tag='#R0'")
        self.assertFalse((await self.service.startup(at=400))["mirrored"])
        self.assertEqual(await self.dump(), before)

    async def test_old_binary_changes_are_authoritative_with_a_history_gap(self):
        await self.service.delete_link("#Q0", operation_key="before-rollback", at=250)
        continuous = await self.rows("SELECT l.* FROM account_links l JOIN accounts a ON a.id=l.account_id "
                                     "WHERE a.player_tag='#R0'")
        with closing(sqlite3.connect(self.legacy)) as c, c:
            c.execute("UPDATE links SET discord_user_id=300 WHERE player_tag='#P0'")
        self.assertEqual((await self.service.read_links())["#P0"]["discord_user_id"], 300)
        report = await self.service.startup(at=400)
        self.assertTrue(report["imported"])
        self.assertEqual((await self.rows("SELECT basis FROM link_imports ORDER BY id DESC LIMIT 1"))[0]["basis"], "rollback_gap")
        self.assertEqual((await self.service.read_links())["#P0"]["discord_user_id"], 300)
        retained = await self.rows("SELECT l.* FROM account_links l JOIN accounts a ON a.id=l.account_id "
                                   "WHERE a.player_tag='#R0'")
        item = (await self.rows("SELECT source_item_id FROM imports"))[0]["source_item_id"]
        self.assertEqual(retained, [{**continuous[0], "last_observed_at": 400, "source_item_id": item}])
        await self.service.startup(at=401)
        self.assertEqual(len(await self.rows("SELECT * FROM link_imports")), 2)
        with self.assertRaisesRegex(LinkConflict, "stale_operation_key"):
            await self.service.delete_link("#Q0", operation_key="before-rollback", at=500)

    async def test_reused_key_unexpected_image_and_aborted_key_do_not_acknowledge_success(self):
        await self.service.upsert_link(**row(owner=300), operation_key="same", at=300)
        with self.assertRaisesRegex(LinkConflict, "different_request"):
            await self.service.upsert_link(**row(owner=400), operation_key="same", at=400)
        with patch.object(self.service, "_apply_legacy", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                await self.service.delete_link("#Q0", operation_key="aborted", at=400)
        await self.service.recover()
        with self.assertRaisesRegex(LinkConflict, "aborted_or_stale"):
            await self.service.delete_link("#Q0", operation_key="aborted", at=500)
        with patch.object(self.service, "_finish", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                await self.service.delete_link("#Q0", operation_key="unexpected", at=500)
        with closing(sqlite3.connect(self.legacy)) as c, c:
            c.execute("UPDATE links SET player_name_last_seen='external' WHERE player_tag='#P0'")
        with self.assertRaisesRegex(LinkConflict, "image_conflict"):
            await self.service.recover()

    async def test_missing_link_refresh_is_a_noop_and_io_runs_on_a_worker(self):
        main_thread = threading.get_ident()
        threads = []
        real_apply = self.service._apply_legacy
        def worker(*args, **kwargs):
            threads.append(threading.get_ident())
            return real_apply(*args, **kwargs)
        before = await self.service.read_links()
        with patch.object(self.service, "_apply_legacy", worker):
            await self.service.update_link_last_seen(player_tag="#V0", player_name_last_seen="Missing",
                  last_seen_clan_tag="", last_seen_clan_code="", last_seen_role="", at=300)
        self.assertEqual(await self.service.read_links(), before)
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], main_thread)

    async def test_unstarted_invalid_inputs_and_external_write_during_commit_are_rejected(self):
        unstarted = LinkService(self.store, self.legacy)
        self.assertEqual(len(await unstarted.read_links()), 3)
        before = await self.dump()
        for call in (lambda: self.service.upsert_link(**row("invalid")),
                     lambda: self.service.restore_links({"#P0": row(owner=0)}),
                     lambda: self.service.update_link_last_seen(player_tag="#P0", last_seen_role="member")):
            with self.assertRaises(ValueError):
                await call()
        self.assertEqual(await self.dump(), before)
        real_apply = self.service._apply_legacy
        def external(*args, **kwargs):
            with closing(sqlite3.connect(self.legacy)) as c, c:
                c.execute("UPDATE links SET discord_user_id=500 WHERE player_tag='#P0'")
            return real_apply(*args, **kwargs)
        with patch.object(self.service, "_apply_legacy", external):
            with self.assertRaisesRegex(LinkConflict, "changed_before_commit"):
                await self.service.upsert_link(**row(owner=300), at=300)
        self.assertEqual(snapshot(self.legacy).links["#P0"]["discord_user_id"], 500)
        with self.assertRaises(LinkConflict):
            await self.service.recover()

    async def test_concurrent_calls_and_cancellation_finish_in_worker_order(self):
        started, release = threading.Event(), threading.Event()
        real_apply = self.service._apply_legacy
        def blocked(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test worker timeout")
            return real_apply(*args, **kwargs)
        with patch.object(self.service, "_apply_legacy", blocked):
            first = asyncio.create_task(self.service.upsert_link(**row(owner=300), at=300))
            await asyncio.to_thread(started.wait, 5)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(self.service.delete_link("#P0", at=400))
            release.set()
            await second
        self.assertNotIn("#P0", await self.service.read_links())
        self.assertEqual([r["status"] for r in await self.rows("SELECT status FROM link_operations ORDER BY id")], ["complete", "complete"])
