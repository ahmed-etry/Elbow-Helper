import unittest
from unittest.mock import patch

from elbow_helper.agent.conversation.instructions import WorkingState


def _remember(state, quote="Keep these accounts together", **changes):
    values = dict(label="Account grouping", quote=quote, request_text=quote,
                  member_id=42, message_id=1, channel_id=100, created_at="2026-09-17")
    values.update(changes)
    return state.remember(**values)


class WorkingStateTests(unittest.TestCase):
    def test_instruction_must_be_a_verbatim_current_request_quote(self):
        with self.assertRaises(ValueError):
            _remember(WorkingState(), request_text="Different request")

    def test_replacement_keeps_original_quote_and_revision_source(self):
        original, first = _remember(WorkingState())
        revised, second = _remember(original, "Separate these accounts", message_id=2, replaces_id=first.instruction_id)
        self.assertTrue(original.instructions[0].active)
        self.assertEqual(revised.instructions[0].quote, first.quote)
        self.assertEqual(revised.instructions[0].retired_by_message_id, 2)
        self.assertEqual(second.supersedes_id, first.instruction_id)
        self.assertEqual(revised.version, 2)

    def test_another_member_cannot_replace_or_retire_an_instruction(self):
        state, instruction = _remember(WorkingState())
        with self.assertRaises(ValueError):
            _remember(state, replaces_id=instruction.instruction_id, member_id=43)
        with self.assertRaises(ValueError):
            state.retire(instruction.instruction_id, member_id=43, message_id=2)
        self.assertTrue(state.instructions[0].active)


    def test_active_limit_rejects_without_evicting_older_constraints(self):
        state, first = _remember(WorkingState())
        with patch("elbow_helper.agent.conversation.instructions.MAX_ACTIVE_INSTRUCTIONS", 1):
            with self.assertRaises(ValueError):
                _remember(state, "Another constraint")
        self.assertEqual(state.instructions, (first,))

    def test_revision_budget_discards_inactive_history_not_active_constraints(self):
        state, first = _remember(WorkingState())
        with patch("elbow_helper.agent.conversation.instructions.MAX_INSTRUCTION_REVISIONS", 1):
            revised, second = _remember(state, "New constraint", replaces_id=first.instruction_id)
        self.assertEqual(revised.instructions, (second,))

    def test_byte_budget_rejects_atomically(self):
        state, first = _remember(WorkingState())
        with patch("elbow_helper.agent.conversation.instructions.MAX_INSTRUCTION_BYTES", 1):
            with self.assertRaises(ValueError):
                _remember(state, "New constraint", replaces_id=first.instruction_id)
        self.assertEqual(state.instructions, (first,))
