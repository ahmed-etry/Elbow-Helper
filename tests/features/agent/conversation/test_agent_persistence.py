import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch

from elbow_helper.features.agent.conversation.state import ConversationStore, ConversationTurn
from elbow_helper.features.agent.conversation.persistence import ConversationPersistence
from elbow_helper.features.agent.conversation.repository import ConversationRepository, StoredConversation
from elbow_helper.features.agent.conversation.transcripts import archive_write


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repository = ConversationRepository(Path(self.directory.name) / "agent.sqlite3")
        self.persistence = ConversationPersistence(self.repository)
        self.store = ConversationStore()
        self.conversation = self.store.create(1, 100, 1)
        self.conversation.append(ConversationTurn("visible context", frozenset({100})))
        self.store.register_reply(self.conversation, 10)

    async def test_restore_then_update_uses_saved_revision(self):
        await self.persistence.save(self.store, self.conversation)
        restored_store = ConversationStore()
        restarted = ConversationPersistence(ConversationRepository(self.repository.path))
        await restarted.restore(restored_store, guild_id=1)
        conversation = restored_store.find(1, 100, 10)
        conversation.append(ConversationTurn("next", frozenset({100})))
        await restarted.save(restored_store, conversation)
        snapshot = self.repository.find_reply(1, 100, 10, now=time.time())
        self.assertEqual(snapshot.revision, 2)

    async def test_foreign_and_invalid_snapshot_are_not_restored(self):
        await self.persistence.save(self.store, self.conversation)
        now = time.time()
        self.repository.save(StoredConversation(1, 100, 2, 0, now, now + 21600, '{"format":999}'), (20,))
        restarted = ConversationPersistence(self.repository)
        restored_store = ConversationStore()
        with self.assertLogs("elbow_helper.features.agent.conversation.persistence", level="ERROR"):
            await restarted.restore(restored_store, guild_id=1)
        self.assertIsNotNone(restored_store.find(1, 100, 10))
        self.assertIsNone(restored_store.find(1, 100, 20))
        foreign_store = ConversationStore()
        await restarted.restore(foreign_store, guild_id=2)
        self.assertEqual(foreign_store.entries(), ())

    async def test_failed_save_does_not_advance_revision(self):
        with patch.object(self.repository, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.persistence.save(self.store, self.conversation)
        await self.persistence.save(self.store, self.conversation)
        self.assertEqual(self.repository.find_reply(1, 100, 10, now=time.time()).revision, 1)

    async def test_capacity_evicts_only_snapshots_no_longer_loaded(self):
        with (
            patch("elbow_helper.features.agent.conversation.state.MAX_CONVERSATIONS", 2),
            patch("elbow_helper.features.agent.conversation.repository.MAX_CONVERSATIONS", 2),
        ):
            second = self.store.create(1, 100, 2)
            second.append(ConversationTurn("second", frozenset({100})))
            self.store.register_reply(second, 20)
            await self.persistence.save(self.store, self.conversation)
            await self.persistence.save(self.store, second)

            # Make the first snapshot newest on disk without changing the
            # store's capacity order, then evict it from memory.
            self.conversation.append(ConversationTurn("newer", frozenset({100})))
            await self.persistence.save(self.store, self.conversation)
            third = self.store.create(1, 100, 3)
            third.append(ConversationTurn("third", frozenset({100})))
            self.store.register_reply(third, 30)
            await self.persistence.save(self.store, third)

            self.assertIsNone(self.repository.find_reply(1, 100, 10, now=time.time()))
            self.assertIsNotNone(self.repository.find_reply(1, 100, 20, now=time.time()))
            second.append(ConversationTurn("still writable", frozenset({100})))
            await self.persistence.save(self.store, second)

        self.assertEqual(self.repository.find_reply(1, 100, 20, now=time.time()).revision, 2)

    async def test_restore_rehydrates_revision_after_memory_eviction(self):
        await self.persistence.save(self.store, self.conversation)
        with patch("elbow_helper.features.agent.conversation.state.MAX_CONVERSATIONS", 1):
            second = self.store.create(1, 100, 2)
        second.append(ConversationTurn("second", frozenset({100})))
        self.store.register_reply(second, 20)
        await self.persistence.save(self.store, second)
        self.assertEqual(self.store.entries()[0][0], 2)
        self.assertIsNotNone(self.repository.find_reply(1, 100, 10, now=time.time()))

        restored_store = ConversationStore()
        await self.persistence.restore(restored_store, guild_id=1)
        restored = restored_store.find(1, 100, 10)
        restored.append(ConversationTurn("after restore", frozenset({100})))
        await self.persistence.save(restored_store, restored)

        self.assertEqual(self.repository.find_reply(1, 100, 10, now=time.time()).revision, 2)

    async def test_exact_reply_lazily_restores_evicted_revision_and_protects_active_state(self):
        await self.persistence.save(self.store, self.conversation)
        with patch("elbow_helper.features.agent.conversation.state.MAX_CONVERSATIONS", 1):
            second = self.store.create(1, 100, 2)
            second.append(ConversationTurn("second", frozenset({100})))
            self.store.register_reply(second, 20)
            second.pending = 1
            with self.assertLogs(
                "elbow_helper.features.agent.conversation.persistence", level="ERROR",
            ):
                blocked = await self.persistence.restore_reply(
                    self.store, guild_id=1, channel_id=100, reply_id=10,
                )
            self.assertIsNone(blocked)
            self.assertIs(self.store.find(1, 100, 20), second)

            second.pending = 0
            restored = await self.persistence.restore_reply(
                self.store, guild_id=1, channel_id=100, reply_id=10,
            )
            self.assertIsNotNone(restored)
            self.assertIsNone(self.store.find(1, 100, 20))
            restored.append(ConversationTurn("continued", frozenset({100})))
            await self.persistence.save(self.store, restored)

        snapshot = self.repository.find_reply(1, 100, 10, now=time.time())
        self.assertEqual(snapshot.revision, 2)

    async def test_malformed_lazy_snapshot_does_not_block_valid_reply_restore(self):
        await self.persistence.save(self.store, self.conversation)
        now = time.time()
        self.repository.save(
            StoredConversation(
                1, 100, 2, 0, now, now + 21600, '{"format":999}',
            ),
            (20,),
        )
        restored_store = ConversationStore()
        with self.assertLogs(
            "elbow_helper.features.agent.conversation.persistence", level="ERROR",
        ):
            invalid = await self.persistence.restore_reply(
                restored_store, guild_id=1, channel_id=100, reply_id=20,
            )
        self.assertIsNone(invalid)
        valid = await self.persistence.restore_reply(
            restored_store, guild_id=1, channel_id=100, reply_id=10,
        )
        self.assertIsNotNone(valid)
        self.assertIs(restored_store.find(1, 100, 10), valid)

    async def test_malformed_bulk_snapshot_does_not_interrupt_later_valid_restore(self):
        await self.persistence.save(self.store, self.conversation)
        now = time.time()
        self.repository.save(
            StoredConversation(
                1, 100, 2, 0, now - 100, now + 21_500,
                '{"format":999}',
            ),
            (20,),
        )
        restored_store = ConversationStore()
        restarted = ConversationPersistence(self.repository)
        with self.assertLogs(
            "elbow_helper.features.agent.conversation.persistence", level="ERROR",
        ):
            await restarted.restore(restored_store, guild_id=1)

        self.assertIsNone(restored_store.find(1, 100, 20))
        self.assertIsNotNone(restored_store.find(1, 100, 10))

    async def test_persistence_prune_preserves_locked_snapshot_until_unlock(self):
        await self.persistence.save(self.store, self.conversation)
        expired = time.time() - 1
        with self.repository.connect() as connection:
            connection.execute(
                "UPDATE conversations SET expires_at=? WHERE root_message_id=?",
                (expired, 1),
            )
            connection.commit()

        async with self.conversation.lock:
            await self.persistence.prune(self.store)
            with self.repository.connect() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM conversations WHERE root_message_id=?",
                    (1,),
                ).fetchone()[0]
            self.assertEqual(count, 1)

        await self.persistence.prune(self.store)
        with self.repository.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM conversations WHERE root_message_id=?",
                (1,),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    async def test_cancellation_settles_started_checkpoint_write(self):
        started, release = Event(), Event()
        original = self.repository.save
        def delayed(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("Test release was not signalled")
            return original(*args, **kwargs)
        with patch.object(self.repository, "save", side_effect=delayed):
            task = asyncio.create_task(self.persistence.save(self.store, self.conversation))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(self.repository.find_reply(1, 100, 10, now=time.time()))
        await self.persistence.save(self.store, self.conversation)
        self.assertEqual(self.repository.find_reply(1, 100, 10, now=time.time()).revision, 2)

    async def test_cancellation_settles_started_transcript_write(self):
        started, release, completed = Event(), Event(), Event()
        def delayed():
            started.set()
            if not release.wait(5):
                raise TimeoutError("Test release was not signalled")
            completed.set()
        task = asyncio.create_task(archive_write(delayed))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(completed.is_set())
