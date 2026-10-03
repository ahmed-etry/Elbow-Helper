"""Action attempts remain visible and cannot be claimed twice."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from elbow_helper.features.agent.actions.store import (
    ACTION_LOG_RETENTION_SECONDS, AgentActionRepository,
)


class ActionRepositoryTests(unittest.TestCase):
    def test_retention_removes_old_runs_steps_and_message_records(self):
        now = ACTION_LOG_RETENTION_SECONDS + 2000
        identifiers = []
        for timestamp in (1999, 2000, 2001):
            run_id = self.repository.create_run(
                guild_id=1, channel_id=2, request_message_id=timestamp,
                requester_id=4, confirmer_id=4,
                steps=({"name": "synthetic", "class": "change", "values": {},
                        "preview": ["Synthetic"]},), now=timestamp,
            )
            self.repository.claim(run_id, owner="worker", now=timestamp)
            self.repository.start_step(run_id, 0, owner="worker", now=timestamp)
            self.repository.finish_step(run_id, 0, owner="worker", status="completed",
                                        outcome={}, now=timestamp)
            self.repository.finish_run(run_id, owner="worker", status="completed", now=timestamp)
            self.repository.record_message(message_id=timestamp, guild_id=1, channel_id=2,
                                           requester_id=4, now=timestamp)
            identifiers.append(run_id)
        self.assertEqual(self.repository.prune_log(now=now), 1)
        self.assertIsNone(self.repository.run(identifiers[0]))
        self.assertIsNone(self.repository.agent_message(message_id=1999, guild_id=1, channel_id=2))
        with self.repository.connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM action_steps WHERE run_id=?", (identifiers[0],)
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        for timestamp, identifier in zip((2000, 2001), identifiers[1:]):
            self.assertIsNotNone(self.repository.run(identifier))
            self.assertIsNotNone(self.repository.agent_message(
                message_id=timestamp, guild_id=1, channel_id=2))

    def test_retention_keeps_recently_updated_runs(self):
        run_id = self.create()
        self.repository.claim(run_id, owner="worker", now=2000)
        self.repository.start_step(run_id, 0, owner="worker", now=2001)
        self.repository.prune_log(now=ACTION_LOG_RETENTION_SECONDS + 2000)
        self.assertIsNotNone(self.repository.run(run_id))
        self.assertEqual(len(self.repository.run(run_id)["steps"]), 1)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repository = AgentActionRepository(Path(self.directory.name) / "agent_actions.sqlite3")

    def create(self):
        return self.repository.create_run(
            guild_id=1, channel_id=2, request_message_id=3,
            requester_id=4, confirmer_id=4,
            steps=({"name": "synthetic_change", "class": "change",
                    "values": {"target": 5}, "preview": ["Change target 5"],
                    "before": {"value": 1}},), now=1000,
        )

    def test_initial_schema_is_complete_and_reopens_without_a_transition(self):
        with self.repository.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            self.assertEqual(tables, {
                "action_runs", "action_steps", "action_log", "agent_messages",
                "saved_requests", "watchers", "member_timezones",
            })
            for table in tables:
                with self.subTest(table=table):
                    columns = {row[1]: row for row in connection.execute(
                        f"PRAGMA table_info({table})"
                    )}
                    self.assertNotIn("lease_expires_at", columns)
                    if "guild_id" in columns:
                        self.assertEqual(columns["guild_id"][3], 1)
        run_id = self.create()
        reopened = AgentActionRepository(self.repository.path)
        self.assertEqual(reopened.run(run_id)["requester_id"], 4)

    def test_claim_and_step_are_single_use_with_a_durable_log(self):
        run_id = self.create()
        self.assertTrue(self.repository.claim(run_id, owner="worker", now=1001))
        self.assertFalse(self.repository.claim(run_id, owner="other", now=1001))
        self.assertTrue(self.repository.start_step(run_id, 0, owner="worker", now=1002))
        self.assertFalse(self.repository.start_step(run_id, 0, owner="worker", now=1002))
        self.assertEqual(self.repository.recent_log(requester_id=4)[0]["outcome"], "started")
        self.assertTrue(self.repository.finish_step(
            run_id, 0, owner="worker", status="completed",
            outcome={"status": "complete"}, after={"value": 2}, now=1003,
        ))
        self.assertFalse(self.repository.finish_step(
            run_id, 0, owner="worker", status="completed", outcome={}, now=1004,
        ))
        self.assertTrue(self.repository.finish_run(run_id, owner="worker", status="completed"))
        self.assertEqual(self.repository.run(run_id)["steps"][0]["status"], "completed")
        self.assertIn('"value": 1', self.repository.recent_log(requester_id=4)[0]["before_json"])
        self.assertIn('"value": 2', self.repository.recent_log(requester_id=4)[0]["after_json"])

    def test_restart_interrupts_running_steps_and_never_reclaims_them(self):
        run_id = self.create()
        self.repository.claim(run_id, owner="worker", now=1001)
        self.repository.start_step(run_id, 0, owner="worker", now=1002)
        interrupted = self.repository.interrupt_incomplete(guild_id=1, now=1003)
        self.assertEqual([run["run_id"] for run in interrupted], [run_id])
        self.assertEqual(self.repository.run(run_id)["steps"][0]["status"], "interrupted")
        self.assertEqual(self.repository.recent_log(requester_id=4)[0]["outcome"], "interrupted")
        self.assertFalse(self.repository.claim(run_id, owner="new-worker", now=1004))
        self.assertEqual(len(self.repository.unreported_interruptions(guild_id=1)), 1)
        self.repository.mark_reported(run_id, now=1005)
        self.assertEqual(self.repository.unreported_interruptions(guild_id=1), [])

    def test_stop_and_log_retention(self):
        run_id = self.create()
        self.repository.claim(run_id, owner="worker", now=1001)
        self.assertFalse(self.repository.request_stop(run_id, requester_id=99))
        self.assertTrue(self.repository.request_stop(run_id, requester_id=4))
        self.assertFalse(self.repository.start_step(run_id, 0, owner="worker", now=1002))
        self.repository.finish_run(run_id, owner="worker", status="stopped")
        self.assertEqual(self.repository.prune_log(now=1003), 0)

    def test_log_is_kept_for_at_least_ninety_days(self):
        run_id = self.create()
        self.repository.claim(run_id, owner="worker", now=1001)
        self.repository.start_step(run_id, 0, owner="worker", now=1002)
        self.assertEqual(self.repository.prune_log(
            now=1002 + ACTION_LOG_RETENTION_SECONDS,
        ), 0)
        self.assertEqual(self.repository.prune_log(
            now=1003 + ACTION_LOG_RETENTION_SECONDS,
        ), 1)

    def test_only_recorded_agent_posts_can_be_edited_or_deleted(self):
        self.assertIsNone(self.repository.agent_message(
            message_id=5, guild_id=1, channel_id=2,
        ))
        self.repository.record_message(
            message_id=5, guild_id=1, channel_id=2, requester_id=4, now=1000,
        )
        self.assertIsNone(self.repository.agent_message(
            message_id=5, guild_id=1, channel_id=3,
        ))
        self.assertEqual(self.repository.agent_message(
            message_id=5, guild_id=1, channel_id=2,
        )["requester_id"], 4)
        self.assertTrue(self.repository.mark_message_deleted(
            message_id=5, guild_id=1, channel_id=2, now=1001,
        ))
        self.assertIsNone(self.repository.agent_message(
            message_id=5, guild_id=1, channel_id=2,
        ))
