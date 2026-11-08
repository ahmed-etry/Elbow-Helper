"""Both research cursor formats keep the same canonical scope hash."""

import unittest

from elbow_helper.agent.research.cursors import scope_digest
from elbow_helper.agent.research.history import _scope_digest as history_digest
from elbow_helper.agent.research.threads import _scope_digest as thread_digest


class ScopeDigestTests(unittest.TestCase):
    def test_existing_unicode_cursor_scope_hash_is_preserved(self):
        # SHA-256 of sorted compact JSON with the default ASCII escaping.
        expected = "27e7d2d443c59d8f"
        for digest in (scope_digest, history_digest, thread_digest):
            self.assertEqual(digest({"z": [1, "é"], "a": True}), expected)
            self.assertEqual(digest({"a": True, "z": [1, "é"]}), expected)
            self.assertNotEqual(digest({"a": False, "z": [1, "é"]}), expected)
            with self.assertRaises(TypeError):
                digest({"invalid": object()})
