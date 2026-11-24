import asyncio
from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
from tests.temp_directory import TemporaryDirectory
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.conversation.state import ConversationRecord, ConversationStore, ConversationTurn
from elbow_helper.agent.engine.capability_contract import CapabilityContract
from elbow_helper.agent.engine.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentToolDefinition
from elbow_helper.infrastructure.ai.client import _chat_messages
from tests.agent.engine.helpers import social_step
from tests.agent.request_fixtures import flow_context as _context
from tests.agent.loop_stub import (
    StubModel, StubSession, banter_answer, call, final, loop_service, without_server_notes,
)
from elbow_helper.agent.conversation.persistence import ConversationPersistence
from elbow_helper.agent.conversation.repository import ConversationRepository, StoredConversation
from elbow_helper.agent.conversation.transcripts import archive_write


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


    async def test_loop_lookups_are_stored_with_the_turn_and_replayed_after_a_restart(self):
        value = RegisteredAgentTool(AgentToolDefinition("read_value", "Synthetic lookup", {
            "type": "object", "properties": {"value": {"type": "integer"}},
            "required": ["value"], "additionalProperties": False,
        }), AsyncMock(return_value={"value": 7}), contract=CapabilityContract(source_scope="request_context"))
        context = _context()
        service = loop_service(StubModel(StubSession(call("read_value", {"value": 7}), final("The value is 7."))))
        service.registry = {"read_value": value}
        with ExitStack() as stack:
            without_server_notes(stack)
            await service.answer(question="What is the value?", context=context)
        lookup, = context.state.lookup_results
        self.assertEqual(json.loads(lookup)["capability"], "read_value")
        self.assertEqual(json.loads(json.loads(lookup)["output"]), {"value": 7})
        record = ConversationRecord(71, 41, "2026-01-01", "What is the value?", "The value is 7.",
                                    "The value is 7.", "", (), (), (10,), True,
                                    lookup_results=tuple(context.state.lookup_results))
        self.conversation.append(ConversationTurn('{"answer":"The value is 7."}', frozenset(), record))
        await self.persistence.save(self.store, self.conversation)

        restored_store = ConversationStore()
        await ConversationPersistence(ConversationRepository(self.repository.path)).restore(restored_store, guild_id=1)
        turn = restored_store.find(1, 100, 10).turns[-1]
        self.assertEqual(turn.record.lookup_results, (lookup,))
        follow_up = StubModel(StubSession(final("Still 7.")))
        followup_context = replace(_context(), history=(turn,))
        followup_service = loop_service(follow_up)
        followup_service.registry = {"read_value": value}
        with ExitStack() as stack:
            without_server_notes(stack)
            await followup_service.answer(question="And now?", context=followup_context)
        self.assertEqual(follow_up.requests[0]["prior_messages"], [
            {"role": "user", "content": "What is the value?"},
            {"type": "function_call", "call_id": "prior_71_0", "name": "read_value", "arguments": '{"value":7}'},
            {"type": "function_call_output", "call_id": "prior_71_0", "output": '{"value":7}'},
            {"role": "assistant", "content": "The value is 7."},
        ])

    async def test_a_banter_reply_turn_is_replayed_as_plain_messages(self):
        banter_context = _context()
        answer, _, _ = await banter_answer(banter_context, social_step("Deal with it.", reactions=("<:sip:1>",)),
                                           question="Use your words")
        self.assertEqual(banter_context.state.lookup_results, [])
        record = ConversationRecord(72, 41, "2026-01-01", "Use your words", answer, answer, "", (), (), (11,),
                                    True, lookup_results=tuple(banter_context.state.lookup_results))
        # An earlier banter turn that sent only a GIF is stored as its file name, with no lookup. The
        # model is told it was its own GIF and what it shows, not shown a name it could take for a member's picture.
        gif = "attachment://reaction-21.webp"
        gif_record = ConversationRecord(70, 41, "2026-01-01", "why are you so salty", gif, gif, "", (), (), (9,),
                                        True, lookup_results=())
        follow_up = StubModel(StubSession(final("Sure.")))
        with ExitStack() as stack:
            without_server_notes(stack)
            await loop_service(follow_up).answer(
                question="And?", context=replace(_context(), history=(
                    ConversationTurn("{}", frozenset(), gif_record), ConversationTurn("{}", frozenset(), record))))
        prior = follow_up.requests[0]["prior_messages"]
        self.assertEqual(prior, [
            {"role": "user", "content": "why are you so salty"},
            {"role": "assistant",
             "content": "[GIF you sent: SpongeBob holding up a phone with a flat, unimpressed stare]"},
            {"role": "user", "content": "Use your words"},
            {"role": "assistant", "content": answer},
        ])
        # DeepSeek rejects any message whose content is not a string or a list.
        self.assertTrue(all(isinstance(message["content"], str) for message in _chat_messages(prior)))

    async def test_a_replied_to_gif_the_bot_posted_is_replayed_as_its_own_gif(self):
        base = _context()
        gif_message = SimpleNamespace(
            id=60, channel=base.source_message.channel, mentions=[], content="",
            author=SimpleNamespace(id=base.guild.me.id, display_name="Elbow Helper"),
            attachments=[SimpleNamespace(filename="reaction-21.webp", content_type="image/webp", size=1)],
            embeds=[], stickers=[], reference=None, guild=base.guild, jump_url="https://discord.test/60",
            created_at=base.source_message.created_at)
        source = base.source_message
        source.reference = SimpleNamespace(message_id=60, channel_id=91, resolved=gif_message)
        source.guild = base.guild
        follow_up = StubModel(StubSession(final("Sure.")))
        with ExitStack() as stack:
            without_server_notes(stack)
            await loop_service(follow_up).answer(question="bruh alright", context=replace(
                base, attachment_sources=(source, gif_message)))
        self.assertEqual(follow_up.requests[0]["prior_messages"], [{
            "role": "assistant", "content": "Elbow Helper (member_id=999): [GIF you sent: SpongeBob holding up a "
                                            "phone with a flat, unimpressed stare]"}])

    async def test_a_confirmed_change_outcome_is_replayed_to_the_follow_up(self):
        outcome = json.dumps({"action_run_id": "run-1", "status": "completed", "actions": [{
            "name": "make_thread", "label": "Make a thread", "status": "completed",
            "target_links": ["https://discord.com/channels/1/2"]}]})
        cancelled = json.dumps({"confirmation": "cancelled", "actions": ["Post message", "Send DM"]})
        follow_up = StubModel(StubSession(final("Done.")))
        context = replace(_context(), history=(ConversationTurn(outcome, frozenset()),
                                               ConversationTurn(cancelled, frozenset())))
        with ExitStack() as stack:
            without_server_notes(stack)
            await loop_service(follow_up).answer(question="Did it work?", context=context)
        # Each is the bot's own, so the model is told so and told whether anything ran.
        self.assertEqual(follow_up.requests[0]["prior_messages"], [
            {"role": "assistant", "content": "[Changes you ran after Confirm, completed: "
                                             "Make a thread (completed) https://discord.com/channels/1/2]"},
            {"role": "assistant", "content": "[Your Confirm card was cancelled, nothing ran: "
                                             "Post message, Send DM]"}])

    async def test_failed_save_does_not_advance_revision(self):
        with patch.object(self.repository, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.persistence.save(self.store, self.conversation)
        await self.persistence.save(self.store, self.conversation)
        self.assertEqual(self.repository.find_reply(1, 100, 10, now=time.time()).revision, 1)


    async def test_exact_reply_lazily_restores_evicted_revision_and_protects_active_state(self):
        await self.persistence.save(self.store, self.conversation)
        with patch("elbow_helper.agent.conversation.state.MAX_CONVERSATIONS", 1):
            second = self.store.create(1, 100, 2)
            second.append(ConversationTurn("second", frozenset({100})))
            self.store.register_reply(second, 20)
            second.pending = 1
            with self.assertLogs(
                "elbow_helper.agent.conversation.persistence", level="ERROR",
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
            "elbow_helper.agent.conversation.persistence", level="ERROR",
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
            "elbow_helper.agent.conversation.persistence", level="ERROR",
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
