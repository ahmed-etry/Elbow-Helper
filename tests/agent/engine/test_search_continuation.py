"""Exercise the second efficiency failures with real scope, scans and output checks."""

import unittest

from elbow_helper.agent.plan.checker import valid_arguments
from elbow_helper.agent.research.history import read_discord_channel_history
from tests.agent.research.test_agent_search import _context
from tests.agent.research.test_history_text_filter import history


class SearchContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cursor_only_continuation_preserves_window_and_rejects_scope_changes(self):
        context = _context()
        rows = history(context, [(1, "post one", (), ()), (2, "post two", (), ())])
        first = await read_discord_channel_history(context, {
            "channel_id": 100, "after": "2026-09-01", "limit": 1,
        })
        arguments = {"channel_id": 100, "cursor": first["coverage"]["next_cursor"], "limit": 1}
        for changed in ({"channel_id": 200}, {"after": "2026-08-01"}, {"author_id": 2}):
            result = await read_discord_channel_history(context, {**arguments, **changed})
            # The refusal names the argument that contradicts the cursor.
            self.assertIn("Drop " + next(iter(changed)) + " to continue it", result["error"])
        self.assertEqual(context.message_search.history_page.await_count, 1)
        # The period the cursor was issued for may be repeated, in another notation.
        repeated = await read_discord_channel_history(context, {**arguments, "after": "2026-09-01T00:00:00+00:00"})
        self.assertEqual(repeated["messages"][0]["message_id"], rows[1].message_id)
        second = await read_discord_channel_history(context, arguments)
        self.assertEqual(second["messages"][0]["message_id"], rows[1].message_id)
        self.assertEqual(second["coverage"]["requested_after"], first["coverage"]["requested_after"])
        other_request = _context()
        self.assertIn("error", await read_discord_channel_history(other_request, arguments))

    async def test_history_window_is_read_whole_however_long_the_messages(self):
        context = _context()
        records = [(1, "chatter " * 120, (), ()) for _ in range(190)]
        records += [(1, "Recruitment post was deleted", (), ())]
        rows = history(context, records)
        arguments = {"channel_id": 100, "after": "2026-09-01", "pages": 8, "limit": 100}
        broad = await read_discord_channel_history(context, arguments)
        self.assertFalse(broad["coverage"]["stopped_at_message_budget"])
        self.assertEqual(context.message_search.history_page.await_count, 2)
        self.assertEqual([row["message_id"] for row in broad["messages"]], [row.message_id for row in rows])
        self.assertIsNone(broad["coverage"]["next_cursor"])


class RepeatedEvidenceTests(unittest.TestCase):
    def test_object_alternatives_preserve_shared_fields_and_base_constraints(self):
        schema = {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["once", "weekly"]},
            "at": {"type": "string"}, "day": {"type": "integer", "minimum": 0, "maximum": 6},
        }, "anyOf": [
            {"properties": {"kind": {"enum": ["once"]}}, "required": ["at"]},
            {"properties": {"kind": {"enum": ["weekly"]}}, "required": ["day"]},
        ], "additionalProperties": False}
        self.assertTrue(valid_arguments({"kind": "once", "at": "2026-10-22"}, schema))
        self.assertTrue(valid_arguments({"kind": "weekly", "day": 3}, schema))
        self.assertFalse(valid_arguments({"kind": "once"}, schema))
        self.assertFalse(valid_arguments({"kind": "weekly", "day": 30}, schema))
        self.assertFalse(valid_arguments({"kind": "once", "at": 3}, schema))
        self.assertFalse(valid_arguments({"kind": "once", "at": "today", "unknown": True}, schema))
