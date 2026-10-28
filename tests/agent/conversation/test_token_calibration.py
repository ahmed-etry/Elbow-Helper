"""Calibration reads recorded usage without creating or changing archives."""

from hashlib import sha256
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from agent.conversation.calibrate_tokens import calibrate


class TokenCalibrationTests(unittest.TestCase):
    def test_archive_is_read_only_and_only_aggregates_escape(self):
        marker = "Synthetic confidential reference " * 100
        record = {
            "rounds": [{"usage": {"prompt_tokens": 1000, "completion_tokens": 100}},
                       {"usage": {"prompt_tokens": 1800, "completion_tokens": 50}}],
            "plans": [{"check": {"ok": True}, "plan": {"steps": [{"id": "read"}]}}],
            "tool_calls": [{"step_id": "read", "result": {"body": marker}}],
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "transcripts.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE operator_debug_records (record_id, record_json)")
                connection.execute("INSERT INTO operator_debug_records VALUES (1, ?)", (json.dumps(record),))
            before = sha256(path.read_bytes()).digest()
            report = calibrate(path)
            self.assertEqual(sha256(path.read_bytes()).digest(), before)
            self.assertEqual(report["records"], 1)
            self.assertEqual(report["selected_transitions"], 1)
            self.assertEqual(report["measured_growth_tokens"], 700)
            self.assertNotIn("confidential", json.dumps(report))

    def test_missing_archive_is_not_created(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "missing.sqlite3"
            with self.assertRaises(sqlite3.OperationalError):
                calibrate(path)
            self.assertFalse(path.exists())
