"""Synthetic bounded membership observations and indexed read plans."""

from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.datasets.membership import read_membership_history, read_observations
from elbow_helper.agent.datasets.sources import DataSource
from elbow_helper.agent.models import AgentTurnState
from elbow_helper.features.clan_health.database.repository import ClanHealthRepository


def ts(day):
    return int(datetime(2026, 10, day, tzinfo=timezone.utc).timestamp())


class MembershipReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "health.sqlite3"
        ClanHealthRepository(self.path).initialize()
        self.codes = list(CLANS)[:2]
        self.context = SimpleNamespace(bot=SimpleNamespace(paths=None), state=AgentTurnState())
        for name, replacement in (
            ("require_evidence_access", AsyncMock()),
            ("SOURCES", (DataSource("health", self.path),)),
        ):
            patcher = patch("elbow_helper.agent.datasets.membership." + name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.arguments = {"after": "2026-10-02", "before": "2026-10-07", "history_after": "2026-10-01"}

    def snapshot(self, day, tags, *, partial=0):
        run = f"run-{day}"
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO report_runs VALUES (?,?,'2026-10','BACKGROUND_ALL',?,0,0)",
                         (run, ts(day), partial))
            conn.executemany(
                "INSERT INTO report_players (run_id,season_key,clan_code,player_tag,player_name,status,flags_json,note) "
                "VALUES (?,'2026-10',?,?,?,'Good','[]','')",
                [(run, code, tag, "Synthetic " + tag) for tag, code in tags],
            )

    async def test_new_returning_and_transfer_observations_include_bounds(self):
        first, second = self.codes
        self.snapshot(1, [("#P0", first), ("#P2", first)])
        self.snapshot(2, [("#P0", first)])
        self.snapshot(3, [("#P0", second), ("#P2", first), ("#P8", first)])
        result = await read_membership_history(self.context, self.arguments)
        events = {row["player_tag"]: row for row in result["events"]}
        self.assertEqual(events["#P0"]["kind"], "family_transfer")
        self.assertEqual(events["#P2"]["kind"], "returning_observation")
        self.assertEqual(events["#P2"]["prior_membership_observed_at"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(events["#P8"]["kind"], "first_observed")
        self.assertTrue(all(not row["first_ever_membership_known"] for row in events.values()))
        self.assertEqual(result["coverage"]["observed_after"], "2026-10-01T00:00:00+00:00")
        self.assertFalse(result["coverage"]["history_complete"])
        self.assertEqual(self.context.state.required_access, set())
        page = await read_membership_history(self.context, {**self.arguments, "limit": 1})
        self.assertEqual(page["next_offset"], 1)
        next_page = await read_membership_history(self.context, {**self.arguments, "offset": 1, "limit": 1})
        self.assertNotEqual(page["events"], next_page["events"])

    async def test_prior_history_outside_bound_and_snapshot_gaps_stay_unknown(self):
        code = self.codes[0]
        self.snapshot(1, [("#P0", code)])
        self.snapshot(2, [("#P2", code)])
        self.snapshot(3, [("#P8", code)], partial=1)
        self.snapshot(6, [("#P0", code)])
        result = await read_membership_history(self.context, {**self.arguments, "history_after": "2026-10-02"})
        event = next(row for row in result["events"] if row["player_tag"] == "#P0")
        self.assertEqual(event["kind"], "first_observed")
        self.assertTrue(event["snapshot_gap"])
        self.assertEqual(result["coverage"]["snapshot_count"], 2)
        self.assertEqual(result["coverage"]["max_snapshot_gap_seconds"], 4 * 86400)
        self.assertTrue(result["limitations"]["earlier_membership_outside_window_unknown"])
        self.assertNotIn("#P8", str(result))

    async def test_empty_invalid_and_clipped_windows_are_explicit(self):
        empty = await read_membership_history(self.context, self.arguments)
        self.assertIsNone(empty["coverage"]["observed_after"])
        self.assertEqual(empty["events"], [])
        for extra in ({"history_after": "2025-01-01"}, {"limit": 26}, {"before": "bad"}):
            self.assertIn("error", await read_membership_history(self.context, {**self.arguments, **extra}))
        self.snapshot(1, [("#P0", self.codes[0])])
        self.snapshot(2, [("#P2", self.codes[0])])
        with patch("elbow_helper.agent.datasets.membership.RUN_LIMIT", 1):
            clipped = await read_membership_history(self.context, self.arguments)
        self.assertTrue(clipped["truncated"])
        self.assertTrue(clipped["coverage"]["snapshot_limit_reached"])
        self.assertEqual(clipped["coverage"]["snapshot_count"], 1)

    def test_representative_history_uses_index_and_bounds_rows(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executemany("INSERT INTO report_runs VALUES (?,?,'2026-10','BACKGROUND_ALL',0,0,0)",
                             [(f"synthetic-{i}", ts(1) + i * 60) for i in range(5000)])
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT run_id,created_ts FROM report_runs "
                "WHERE scope='BACKGROUND_ALL' AND partial=0 AND created_ts>=? AND created_ts<? "
                "ORDER BY created_ts DESC,run_id DESC LIMIT 129", (ts(1), ts(7)),
            ).fetchall()
        self.assertIn("idx_report_runs_membership", str(plan))
        runs, rows, clipped = read_observations(self.path, ts(1), ts(7))
        self.assertEqual(len(runs), 128)
        self.assertTrue(clipped)
        self.assertEqual(rows, [])
