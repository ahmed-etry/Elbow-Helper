from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from elbow_helper.features.agent.conversation.transcripts import TranscriptArchive


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "transcripts.sqlite3"
        self.archive = TranscriptArchive(self.path)

    def request(self, **changes):
        values = dict(message_id=1, guild_id=2, channel_id=3, root_message_id=1,
                      member_id=4, created_at="2026-09-17", content="My question", replied_to_message_id=None)
        values.update(changes)
        self.archive.record_request(**values)

    def test_visible_exchange_survives_reopen_without_internal_state(self):
        self.request()
        self.archive.record_reply(message_id=10, request_message_id=1, content="Visible answer")
        reopened = TranscriptArchive(self.path)
        rows = reopened.read_page(guild_id=2, channel_id=3, root_message_id=1)
        self.assertEqual(rows[0]["content"], "My question")
        self.assertEqual(rows[0]["replies"][0]["content"], "Visible answer")
        with reopened.connect() as connection:
            columns = {row[1] for table in ("requests", "replies") for row in connection.execute(f"PRAGMA table_info({table})")}
        self.assertTrue(columns.isdisjoint({"reasoning_content", "local_context", "reports", "system_prompt", "evidence"}))

    def test_duplicates_are_idempotent_and_conflicts_are_rejected(self):
        self.request()
        self.request()
        with self.assertRaises(ValueError):
            self.request(content="Changed")
        self.archive.record_reply(message_id=10, request_message_id=1, content="Answer")
        self.archive.record_reply(message_id=10, request_message_id=1, content="Answer")
        with self.assertRaises(ValueError):
            self.archive.record_reply(message_id=10, request_message_id=1, content="Changed")
        self.assertEqual(len(self.archive.read_page(guild_id=2, channel_id=3, root_message_id=1)), 1)

    def test_scoped_paging_and_unanswered_request(self):
        self.request()
        self.request(message_id=2, content="Next")
        page = self.archive.read_page(guild_id=2, channel_id=3, root_message_id=1, limit=1)
        self.assertEqual(page[0]["replies"], [])
        next_page = self.archive.read_page(guild_id=2, channel_id=3, root_message_id=1, after_message_id=1)
        self.assertEqual(next_page[0]["message_id"], 2)
        self.assertEqual(self.archive.read_page(guild_id=9, channel_id=3, root_message_id=1), ())
        with self.assertRaises(sqlite3.IntegrityError):
            self.archive.record_reply(message_id=99, request_message_id=999, content="orphan")
