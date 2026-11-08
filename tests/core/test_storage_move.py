"""Synthetic rehearsal of the stopped-writer store move and shared owners."""
from contextlib import closing
import json
import asyncio
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import get_ident
import unittest
from unittest.mock import patch

from elbow_helper import storage_move as move
from elbow_helper.infrastructure.persistence.documents import (
    Document, DocumentConflict, read_document, write_document, save_bonus_revision,
)
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration, run_sqlite_migrations
from tests.temp_directory import TemporaryDirectory


class StorageMoveTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "data"
        self.root.mkdir()

    def put(self, name, payload):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def old_agent(self, name, steps):
        path = self.root / "agent" / name
        path.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(path)) as connection:
            local = tuple(SQLiteMigration(i, s.name, s.apply) for i, s in enumerate(steps, 1))
            run_sqlite_migrations(connection, local, target_version=len(local))
        return path

    def old_links(self, with_links_table=True, version=1):
        path = self.root / "clan_links" / "links.sqlite3"
        path.parent.mkdir()
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE suggestions (player_tag TEXT PRIMARY KEY, player_name TEXT NOT NULL,"
                               " current_clan_code TEXT NOT NULL, current_clan_tag TEXT NOT NULL,"
                               " proposed_discord_user_id INTEGER NOT NULL, proposed_display_name TEXT DEFAULT '',"
                               " review_channel_id INTEGER DEFAULT 0, review_message_id INTEGER DEFAULT 0)")
            connection.execute("CREATE TABLE ignored_tags (player_tag TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO suggestions VALUES ('#TESTA','Alpha','AAA','#C1',111,'Alpha',5,6)")
            connection.execute("INSERT INTO ignored_tags VALUES ('#TESTB')")
            if with_links_table:
                connection.execute("CREATE TABLE links (player_tag TEXT, discord_user_id INTEGER)")
                connection.execute("INSERT INTO links VALUES ('#TESTSTALE', 999)")
            connection.execute(f"PRAGMA user_version={version}")
        return path


    def test_documents_and_bonus_revision_are_atomic_and_reject_stale_writers(self):
        move.prepare_storage(self.root)
        database = self.root / "workflows.sqlite3"
        current, previous, audit = [Document(name, database) for name in
                                    ("cwl_bonus_rules", "cwl_bonus_previous", "cwl_bonus_audit")]
        before, after = {"revision": 1}, {"revision": 2}
        write_document(current, before)
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TRIGGER refuse_audit BEFORE INSERT ON feature_documents WHEN NEW.name='cwl_bonus_audit' BEGIN SELECT RAISE(ABORT,'forced'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            save_bonus_revision(current, previous, audit, before, after, {"revision": 2}, 1, 500)
        self.assertEqual(read_document(current), before)
        self.assertFalse(previous.exists())
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("DROP TRIGGER refuse_audit")
        save_bonus_revision(current, previous, audit, before, after, {"revision": 2}, 1, 500)
        with self.assertRaises(DocumentConflict):
            save_bonus_revision(current, previous, audit, before, after, {}, 1, 500)
        self.assertEqual(read_document(previous), before)
        self.assertEqual(len(read_document(audit)["entries"]), 1)

    def test_missing_family_evidence_fails_instead_of_rendering_empty_board(self):
        move.prepare_storage(self.root)
        doc = Document("wars", self.root / "workflows.sqlite3")
        write_document(doc, {"war_board_history": {"BEH": [1]}})
        with closing(sqlite3.connect(self.root / "family.sqlite3")) as connection, connection:
            connection.execute("DELETE FROM saved_board_evidence")
        with self.assertRaisesRegex(ValueError, "family board evidence"):
            read_document(doc)

    def test_all_extensions_start_offline_on_an_empty_layout_with_sqlite_in_workers(self):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("storage_boot.py")),
                                 str(self.root.parent)], capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"extensions": 22', result.stdout)


class NewDocumentPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_namespaces_do_not_use_the_leadership_news_deletion_shape(self):
        from elbow_helper.data.events import EventsStore
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        await asyncio.to_thread(move.prepare_storage, root)
        path = root / "workflows.sqlite3"
        documents = {
            "role_connections": {"connections": [{"target_role_id": 123}]},
            "activity_schedule": {"events": [{"name": "Test"}]},
            "cwl_bonus_audit": {"entries": [{"actor_discord_id": 123, "actor_display": "Name", "revision": 7}]},
            "wars": {"war_role_state": {"BEH": {"lineup": {"#P0Y": 123}, "managed": [123, 456]}}},
        }
        for name, payload in documents.items():
            await asyncio.to_thread(write_document, Document(name, path), payload)
        store = EventsStore(path)
        await store.delete_member(123)
        self.assertEqual(read_document(Document("role_connections", path)), documents["role_connections"])
        self.assertEqual(read_document(Document("activity_schedule", path)), documents["activity_schedule"])
        self.assertIsNone(read_document(Document("cwl_bonus_audit", path))["entries"][0]["actor_display"])
        self.assertEqual(read_document(Document("wars", path))["war_role_state"]["BEH"],
                         {"lineup": {}, "managed": [456]})


class BonusDocumentTests(unittest.IsolatedAsyncioTestCase):
    async def test_rule_editor_reads_memory_and_publishes_a_complete_revision(self):
        from types import SimpleNamespace
        from elbow_helper.features.cwl.bonus import config as bonus
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        await asyncio.to_thread(move.prepare_storage, root)
        database = root / "workflows.sqlite3"
        locations = {name: Document(namespace, database) for name, namespace in (
            ("BONUS_CONFIG_FILE", "cwl_bonus_rules"),
            ("BONUS_CONFIG_BACKUP_FILE", "cwl_bonus_previous"),
            ("BONUS_CONFIG_AUDIT_FILE", "cwl_bonus_audit"))}
        with patch.multiple(bonus, **locations):
            repository = bonus.BonusConfigRepository()
            valid = repository._default_bonus_config()
            for rules in valid["clans"].values():
                rules["matchup_expected"] = {key: 1.5 for key in rules["matchup_expected"]}
            write_document(locations["BONUS_CONFIG_FILE"], valid)
            config, errors = await asyncio.to_thread(repository.load)
            self.assertFalse(errors)
            clan = next(iter(config["clans"]))
            payload = config["clans"][clan]
            await asyncio.to_thread(repository.save_clan, clan, payload,
                                    SimpleNamespace(id=123, display_name="Actor"),
                                    expected_revision=config["revision"], summary="Saved")
            with patch("sqlite3.connect", side_effect=AssertionError("UI read touched SQLite")):
                latest, errors = repository.load()
                self.assertEqual(latest["revision"], config["revision"] + 1)
                self.assertEqual(repository.history(clan)[1], 1)
                self.assertIsNotNone(repository.cache_revision())
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TRIGGER fail_rules BEFORE UPDATE ON feature_documents WHEN NEW.name='cwl_bonus_audit' BEGIN SELECT RAISE(ABORT,'forced'); END")
            with self.assertRaisesRegex(RuntimeError, "Nothing was changed"):
                await asyncio.to_thread(repository.save_clan, clan, payload,
                                        SimpleNamespace(id=123, display_name="Actor"),
                                        expected_revision=latest["revision"], summary="Failed")
            self.assertEqual(read_document(locations["BONUS_CONFIG_FILE"])["revision"], latest["revision"])


class MutationTests(unittest.TestCase):
    def test_concurrent_role_rule_edits_preserve_both_changes(self):
        from concurrent.futures import ThreadPoolExecutor
        from elbow_helper.features.role_connections.cog import RoleConnections
        import time
        cog = object.__new__(RoleConnections)
        cog.state = {"connections": []}
        with patch("elbow_helper.features.role_connections.cog.save_state", side_effect=lambda state: time.sleep(0.01)):
            with ThreadPoolExecutor(max_workers=2) as workers:
                list(workers.map(cog.add_connection, ({"id": "first"}, {"id": "second"})))
        self.assertEqual({row["id"] for row in cog.state["connections"]}, {"first", "second"})


class StartupWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_hook_prepares_storage_in_worker_before_loading_extensions(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from elbow_helper.core.lifecycle import ElbowHelperBot
        seen = []
        loop_thread = get_ident()
        bot = SimpleNamespace(prepare_storage=lambda: seen.append(get_ident()),
                              workflow_events=SimpleNamespace(initialize=AsyncMock()),
                              tree=object(), guild_id=1, required_extensions=(), optional_extensions=())
        with patch("elbow_helper.core.lifecycle.load_extensions", new=AsyncMock()), patch(
                "elbow_helper.core.lifecycle.sync_guild_commands", new=AsyncMock(return_value=())):
            await ElbowHelperBot.setup_hook(bot)
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], loop_thread)
