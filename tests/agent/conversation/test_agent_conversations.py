from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from elbow_helper.agent.conversation.state import (
    CONVERSATION_IDLE_SECONDS, ConversationRecord, ConversationStore,
    ConversationTurn,
)


class ConversationStoreTests(unittest.TestCase):
    def test_posting_an_answer_preserves_an_uncertain_preview_without_recording_it_as_delivered(self):
        conversation = ConversationStore().create(121, 221, 321)
        conversation.append(ConversationTurn("Synthetic turn", frozenset(), record=ConversationRecord(
            request_message_id=321, member_id=121, created_at="2026-01-01",
            question="Synthetic mixed request", generated_answer="Synthetic answer and preview",
            delivered_answer="Synthetic private notice", local_context="", evidence=(), report_ids=(),
            reply_ids=(421,), delivery_complete=False, delivery_unknown=True,
            attempted_nonces=(801, 0), uncertain_nonce=0,
            generated_parts=("Synthetic private notice", "Synthetic preview"),
        )))
        conversation.record_answer_delivery(
            321, "Synthetic answer", (421,), True, False, (901,), None,
            replaced_reply_id=421, generated_parts=("Synthetic answer",),
        )
        record = conversation.turns[0].record
        self.assertEqual(record.delivered_answer, "Synthetic answer")
        self.assertEqual(record.generated_parts, ("Synthetic answer", "Synthetic preview"))
        self.assertFalse(record.delivery_complete)
        self.assertTrue(record.delivery_unknown)
        self.assertEqual(record.uncertain_nonce, 0)
        record = conversation.reconcile_unknown_delivery(
            request_message_id=321, reply_id=422, nonce=0,
            delivered_part="Synthetic preview", delivery_complete=True,
        )
        self.assertEqual(record.delivered_answer, "Synthetic answer\nSynthetic preview")
        self.assertTrue(record.delivery_complete)

    def test_restore_reindexes_replies_without_refreshing_idle_time(self):
        original = ConversationStore()
        conversation = original.create(1, 100, 1)
        original.register_reply(conversation, 10)
        touched = conversation.touched_at
        restored = ConversationStore()
        restored.restore(1, conversation)
        self.assertIs(restored.find(1, 100, 10), conversation)
        self.assertEqual(conversation.touched_at, touched)
        self.assertEqual(restored.entries(), ((1, conversation),))

    def test_restore_conflicting_reply_is_atomic(self):
        store = ConversationStore()
        first = store.create(1, 100, 1)
        store.register_reply(first, 10)
        other = ConversationStore().create(1, 100, 2)
        other.reply_ids = [20, 10]
        with self.assertRaises(ValueError):
            store.restore(2, other)
        self.assertIsNone(store.find(1, 100, 20))
        self.assertIs(store.find(1, 100, 10), first)
        self.assertEqual(len(store.entries()), 1)

    def test_restore_does_not_replace_loaded_or_expired_conversation(self):
        store = ConversationStore()
        first = store.create(1, 100, 1)
        with self.assertRaises(ValueError):
            store.restore(1, first)
        second = ConversationStore().create(1, 100, 2)
        second.touched_at = 0
        with patch("elbow_helper.agent.conversation.state.time.monotonic", return_value=21600):
            with self.assertRaises(ValueError):
                store.restore(2, second)


    def test_repeated_root_event_keeps_existing_conversation(self):
        store = ConversationStore()
        conversation = store.create(1, 100, 1)
        conversation.append(ConversationTurn("existing", frozenset({100})))
        self.assertIs(store.create(1, 100, 1), conversation)
        with self.assertRaises(ValueError):
            store.create(2, 100, 1)

    def test_reply_registration_is_idempotent_and_rejects_reassignment(self):
        store = ConversationStore()
        first = store.create(1, 100, 1)
        second = store.create(1, 100, 2)
        store.register_reply(first, 10)
        store.register_reply(first, 10)
        self.assertEqual(first.reply_ids, [10])
        with self.assertRaises(ValueError):
            store.register_reply(second, 10)
        self.assertIs(store.find(1, 100, 10), first)

    def test_capacity_evicts_least_recently_replied_inactive_conversation(self):
        store = ConversationStore()
        with patch("elbow_helper.agent.conversation.state.MAX_CONVERSATIONS", 2):
            first = store.create(1, 10, 100)
            store.register_reply(first, 101)
            second = store.create(1, 10, 200)
            store.register_reply(second, 201)
            store.register_reply(first, 102)
            third = store.create(1, 10, 300)
            store.register_reply(third, 301)

        self.assertIs(store.find(1, 10, 101), first)
        self.assertIs(store.find(1, 10, 102), first)
        self.assertIsNone(store.find(1, 10, 201))
        self.assertIs(store.find(1, 10, 301), third)


    def test_replies_resolve_only_in_the_original_guild_and_channel(self):
        store = ConversationStore()
        first = store.create(1, 10, 100)
        second = store.create(1, 10, 200)
        store.register_reply(first, 101)
        store.register_reply(second, 201)
        self.assertIs(store.find(1, 10, 101), first)
        self.assertIs(store.find(1, 10, 201), second)
        self.assertIsNone(store.find(2, 10, 101))
        self.assertIsNone(store.find(1, 20, 101))
        self.assertIsNone(store.find(1, 10, 100))


    def test_locked_conversations_are_not_expired_or_evicted(self):
        store = ConversationStore()
        conversation = store.create(1, 10, 100)
        store.register_reply(conversation, 101)
        conversation.touched_at = 0
        with (
            patch("elbow_helper.agent.conversation.state.MAX_CONVERSATIONS", 1),
            patch("elbow_helper.agent.conversation.state.time.monotonic", return_value=30_000),
        ):
            async def verify():
                async with conversation.lock:
                    self.assertIs(store.find(1, 10, 101), conversation)
                    self.assertEqual(store.protected_roots(), (100,))
                    self.assertEqual(store.loaded_roots(), (100,))
                    with self.assertRaises(RuntimeError):
                        store.create(1, 10, 200)

            asyncio.run(verify())

        with patch(
            "elbow_helper.agent.conversation.state.time.monotonic",
            return_value=30_000,
        ):
            self.assertIsNone(store.find(1, 10, 101))

    def test_exact_idle_boundary_expires_only_inactive_conversations(self):
        store = ConversationStore()
        conversation = store.create(1, 10, 100)
        store.register_reply(conversation, 101)
        conversation.touched_at = 1_000
        boundary = 1_000 + CONVERSATION_IDLE_SECONDS

        with patch(
            "elbow_helper.agent.conversation.state.time.monotonic",
            return_value=boundary,
        ):
            self.assertIsNone(store.find(1, 10, 101))

        active_store = ConversationStore()
        active = active_store.create(1, 10, 200)
        active_store.register_reply(active, 201)
        active.touched_at = 1_000
        active.pending = 1
        with patch(
            "elbow_helper.agent.conversation.state.time.monotonic",
            return_value=boundary,
        ):
            self.assertIs(active_store.find(1, 10, 201), active)


    def test_retention_enforces_encoded_byte_budget(self):
        conversation = ConversationStore().create(1, 10, 100)
        with patch("elbow_helper.agent.conversation.state.MAX_RETAINED_BYTES", 30):
            conversation.append(ConversationTurn("é" * 10, frozenset({10})))
            conversation.append(ConversationTurn("later", frozenset({10})))
        self.assertEqual([turn.text for turn in conversation.turns], ["later"])
        self.assertEqual(conversation.evicted_turns, 1)


    def test_unknown_delivery_can_be_reconciled_exactly_once(self):
        conversation = ConversationStore().create(1, 10, 100)
        nonce = 123
        record = ConversationRecord(
            request_message_id=77, member_id=42, created_at="2026-09-18",
            question="Question", generated_answer="Answer",
            delivered_answer="", local_context="", evidence=(),
            report_ids=(), reply_ids=(), delivery_complete=False,
            delivery_unknown=True, attempted_nonces=(nonce,),
            uncertain_nonce=nonce,
        )
        conversation.append(ConversationTurn(
            '{"answer": ""}', frozenset({10}), record=record,
        ))
        previous_version = conversation.version

        with patch(
            "elbow_helper.agent.conversation.state.time.monotonic",
            return_value=1234,
        ):
            updated = conversation.reconcile_unknown_delivery(
                request_message_id=77, reply_id=78, nonce=nonce,
                delivered_part="Answer", delivery_complete=True,
            )

        self.assertEqual(updated.delivered_answer, "Answer")
        self.assertEqual(updated.reply_ids, (78,))
        self.assertTrue(updated.delivery_complete)
        self.assertFalse(updated.delivery_unknown)
        self.assertIsNone(updated.uncertain_nonce)
        self.assertEqual(conversation.version, previous_version + 1)
        self.assertEqual(conversation.touched_at, 1234)
        with self.assertRaises(ValueError):
            conversation.reconcile_unknown_delivery(
                request_message_id=77, reply_id=78, nonce=nonce,
                delivered_part="Answer", delivery_complete=True,
            )

    def test_unknown_delivery_reconciliation_rejects_wrong_nonce(self):
        conversation = ConversationStore().create(1, 10, 100)
        record = ConversationRecord(
            request_message_id=77, member_id=42, created_at="2026-09-18",
            question="Question", generated_answer="Answer",
            delivered_answer="", local_context="", evidence=(),
            report_ids=(), reply_ids=(), delivery_complete=False,
            delivery_unknown=True, attempted_nonces=(123,),
            uncertain_nonce=123,
        )
        conversation.append(ConversationTurn(
            '{"answer": ""}', frozenset({10}), record=record,
        ))

        with self.assertRaises(ValueError):
            conversation.reconcile_unknown_delivery(
                request_message_id=77, reply_id=78, nonce=456,
                delivered_part="Answer", delivery_complete=True,
            )
        self.assertIs(conversation.turns[0].record, record)

    def test_oversized_full_record_keeps_valid_excerpt_and_marks_retention_limit(self):
        conversation = ConversationStore().create(1, 10, 100)
        record = ConversationRecord(
            request_message_id=77, member_id=42, created_at="2026-09-16",
            question="Question", generated_answer="x" * 1000,
            delivered_answer="Answer", local_context="", evidence=(),
            report_ids=(), reply_ids=(78,), delivery_complete=True,
        )
        with (
            patch("elbow_helper.agent.conversation.state.MAX_RETAINED_BYTES", 100),
            self.assertLogs("elbow_helper.agent.conversation.state", level="WARNING"),
        ):
            conversation.append(ConversationTurn('{"answer": "Answer"}', frozenset({10}), record=record))
        self.assertTrue(conversation.turns[0].retention_limited)
        self.assertIsNone(conversation.turns[0].record)
        self.assertEqual(conversation.turns[0].text, '{"answer": "Answer"}')
