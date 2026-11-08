import asyncio
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
import sys
from tests.temp_directory import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from elbow_helper.configuration.files import EVENTS_DATABASE, SQLITE_DATABASES
from elbow_helper.data.events import Event, EventsStore, load_state, record, save_state
from elbow_helper.infrastructure.persistence.backups import backup_database
from elbow_helper.infrastructure.persistence.migrations import UnsupportedSQLiteVersionError


def publication(**values):
    return Event("leadership_news", "7", "news_published", 100,
                 subject_discord_id=41, actor_discord_id=42, actor_kind="member", via="button",
                 detail={"channel_id": 8, "message_id": 9, "guild_id": 10}, **values)


class WorkflowEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch("elbow_helper.infrastructure.persistence.backups.TemporaryDirectory", TemporaryDirectory))
        self.directory = TemporaryDirectory(dir=Path(__file__).parents[2])
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "events.sqlite3"
        self.store = EventsStore(self.path)

    async def test_state_and_event_commit_together_and_retry_has_one_event(self):
        def write(connection):
            save_state(connection, "leadership_news", {"prompts": {}})
            return record(connection, publication())
        self.assertTrue(await self.store.call(write))
        self.assertFalse(await self.store.call(write))
        self.assertEqual(await self.store.call(load_state, "leadership_news"), {"prompts": {}})
        rows = await self.store.read(workflow="leadership_news", kind="news_published", subject=41,
                                     case="7", since=100, until=100)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["detail"], publication().detail)
        self.assertGreater(rows[0]["recorded_at"], 100)
        self.assertEqual(await self.store.read(until=99), [])
        self.assertEqual(await self.store.read(since=101), [])

    async def test_failed_state_or_event_rolls_back_both_in_real_file(self):
        await self.store.initialize()
        for event_first in (False, True):
            def write(connection):
                if event_first:
                    record(connection, publication())
                save_state(connection, "leadership_news", {"prompts": {}})
                if not event_first:
                    record(connection, replace(publication(), detail={"text": "forbidden"}))
                raise RuntimeError("interrupted before commit")
            with self.assertRaises((RuntimeError, ValueError)):
                await self.store.call(write)
            self.assertIsNone(await self.store.call(load_state, "leadership_news"))
            self.assertEqual(await EventsStore(self.path).read(), [])


    async def test_closed_vocabulary_rejects_free_text_and_bad_identifiers(self):
        variants = [replace(publication(), **change) for change in (
            {"workflow": "unknown"}, {"kind": "unknown"}, {"actor_kind": "text"},
            {"via": "text"}, {"detail": {"channel_id": 8, "message_id": "text", "guild_id": 10}},
            {"detail": {}}, {"subject_discord_id": True}, {"clan_code": "text"},
            {"case_key": "member words"}, {"subject_tag": "invalid"}, {"occurred_at": -1},
            {"actor_kind": "import"}, {"actor_discord_id": None},
        )]
        for event in variants:
            with self.subTest(event=event), self.assertRaises(ValueError):
                await self.store.call(record, event)
        self.assertEqual(await self.store.read(), [])

    async def test_one_lock_serializes_worker_calls_even_after_caller_cancellation(self):
        started, release = threading.Event(), threading.Event()
        loop_thread = threading.get_ident()
        observed = []
        def slow(connection):
            observed.append(threading.get_ident())
            started.set()
            release.wait(3)
            record(connection, publication())
        first = asyncio.create_task(self.store.call(slow))
        self.assertTrue(await asyncio.to_thread(started.wait, 3))
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        queued = threading.Event()
        original_call = self.store._call
        def observe_queue(*args):
            queued.set()
            return original_call(*args)
        self.enterContext(patch.object(self.store, "_call", side_effect=observe_queue))
        second = asyncio.create_task(self.store.read())
        try:
            self.assertTrue(await asyncio.to_thread(queued.wait, 3))
            self.assertFalse(second.done())
        finally:
            release.set()
        self.assertEqual(len(await second), 1)
        self.assertNotEqual(observed, [loop_thread])

    async def test_delete_member_removes_subject_actor_and_open_work(self):
        def seed(connection):
            record(connection, publication())
            record(connection, replace(publication(), case_key="11", subject_discord_id=43,
                                       actor_discord_id=44))
            save_state(connection, "transfers", {"clans": {"BEH": {"pending": [
                {"user_id": 41}, {"user_id": 43}]}}})
            save_state(connection, "leadership_news", {"prompts": {
                "1": {"subject_discord_id": 41}, "2": {"subject_discord_id": 43}}})
        await self.store.call(seed)
        self.assertEqual(await self.store.delete_member(42), 1)
        await self.store.delete_member(41)
        transfers = await self.store.call(load_state, "transfers")
        self.assertEqual(transfers["clans"]["BEH"]["pending"], [{"user_id": 43}])
        news = await self.store.call(load_state, "leadership_news")
        self.assertEqual(set(news["prompts"]), {"2"})
        self.assertEqual(len(await self.store.read()), 1)

    async def test_backup_inventory_and_copy_include_state_and_events(self):
        self.assertIn(EVENTS_DATABASE, SQLITE_DATABASES)
        def write(connection):
            save_state(connection, "leadership_news", {"prompts": {}})
            record(connection, publication())
        await self.store.call(write)
        destination = self.path.parent / "backup.sqlite3"
        await asyncio.to_thread(backup_database, self.path, destination)
        restored = EventsStore(destination)
        self.assertEqual(len(await restored.read()), 1)
        self.assertEqual(await restored.call(load_state, "leadership_news"), {"prompts": {}})

    async def test_newer_version_and_missing_database_are_refused(self):
        await self.store.initialize()
        def newer():
            with closing(sqlite3.connect(self.path)) as connection:
                connection.execute("PRAGMA user_version=99")
        await asyncio.to_thread(newer)
        with self.assertRaises(UnsupportedSQLiteVersionError):
            await EventsStore(self.path).initialize()
        await asyncio.to_thread(self.path.unlink)
        with self.assertRaises(FileNotFoundError):
            await self.store.read()


    async def test_process_crash_cannot_commit_only_state_or_only_an_event(self):
        await self.store.initialize()
        for first in ("state", "event"):
            script = """
import asyncio, os, sys
from pathlib import Path
from elbow_helper.data.events import EventsStore, Event, record, save_state
def crash(connection):
    if sys.argv[2] == 'state':
        save_state(connection, 'leadership_news', {'prompts': {}})
    else:
        record(connection, Event('leadership_news', '7', 'news_published', 100,
               detail={'channel_id': 8, 'message_id': 9, 'guild_id': 10}))
    os._exit(17)
asyncio.run(EventsStore(Path(sys.argv[1])).call(crash))
"""
            result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(self.path), first],
                                             capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 17, result.stderr.decode(errors="replace"))
            restarted = EventsStore(self.path)
            self.assertIsNone(await restarted.call(load_state, "leadership_news"))
            self.assertEqual(await restarted.read(), [])

    async def test_corrections_append_without_changing_the_original(self):
        await self.store.call(record, publication())
        first = (await self.store.read())[0]
        correction = replace(publication(), kind="voided", occurred_at=101, detail={"voids": first["id"]})
        await self.store.call(record, correction)
        self.assertEqual((await self.store.read())[0], first)
        self.assertEqual((await self.store.read(kind="voided"))[0]["detail"], {"voids": first["id"]})
