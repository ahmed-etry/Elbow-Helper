"""Leadership record coverage uses synthetic data and never implies conduct."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.capabilities.records.data import SOURCES
from elbow_helper.agent.datasets.query import run_query, query_bot_data
from elbow_helper.agent.datasets.tools import dataset_tools
from elbow_helper.agent.models import AgentTurnState
from elbow_helper.agent.plan.results import model_result
from elbow_helper.features.records.database.repository import RecordRepository


class RecordCoverageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.paths = SimpleNamespace(project_root=root, data_root=root)
        self.db = root / "records.sqlite3"
        RecordRepository(self.db).initialize()
        self.sources = (replace(SOURCES[0], path=self.db),)
        self.window = {"records_after_ts": 100, "records_before_ts": 200}

    def seed(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.executemany(
                "INSERT INTO leadership_records (created_ts,updated_ts,member_id,"
                "member_display,category_key,incident_type_key,note,recorder_id,"
                "recorder_display,status) VALUES (?,?,?,'Synthetic','war','miss',"
                "'Synthetic case',99,'Synthetic recorder',?)",
                [(ts, ts, member, status) for ts, member, status in (
                    (100, 10, "active"), (110, 10, "active"), (120, 20, "removed"),
                    (130, 20, "active"), (200, 30, "active"), (50, 40, "removed"),
                )],
            )
            connection.commit()

    def query(self, sql, params=None, max_rows=200, levels=frozenset({"lead_plus"})):
        return run_query(self.sources, self.paths, levels, sql, params, max_rows)

    def coverage(self, result):
        self.assertNotIn("error", result)
        coverage = result["source_coverage"]["records"]
        self.assertFalse(coverage["coverage_complete"])
        self.assertIn("Sparse or missing records are not evidence", coverage["note"])
        self.assertIn("most leadership does not use them yet", coverage["note"])
        return coverage

    def test_empty_database_retains_plain_absence_caution(self):
        result, touched = self.query("SELECT * FROM records.leadership_records")
        coverage = self.coverage(model_result(result))
        self.assertEqual(result["rows"], [])
        self.assertEqual(coverage["record_count"], 0)
        self.assertEqual(coverage["member_count"], 0)
        self.assertEqual(coverage["members"], [])
        self.assertEqual(coverage["window"], {"after_ts": None, "before_ts": None})
        self.assertFalse(touched)

    def test_sparse_window_counts_members_and_separates_removed_cases(self):
        self.seed()
        result, _ = self.query(
            "SELECT id FROM records.leadership_records "
            "WHERE created_ts >= :records_after_ts AND created_ts < :records_before_ts",
            self.window,
        )
        coverage = self.coverage(result)
        self.assertEqual(coverage["window"], {"after_ts": 100, "before_ts": 200})
        self.assertEqual((coverage["record_count"], coverage["member_count"]), (4, 2))
        self.assertEqual(coverage["members"], [
            {"member_id": 10, "record_count": 2,
             "active_record_count": 2, "removed_record_count": 0},
            {"member_id": 20, "record_count": 2,
             "active_record_count": 1, "removed_record_count": 1},
        ])
        self.assertFalse(coverage["members_truncated"])

    def test_empty_member_result_does_not_erase_other_members_coverage(self):
        self.seed()
        result, _ = self.query(
            "SELECT id FROM records.leadership_records WHERE member_id = :member "
            "AND created_ts >= :records_after_ts AND created_ts < :records_before_ts",
            {**self.window, "member": 999},
        )
        self.assertEqual(result["rows"], [])
        coverage = self.coverage(result)
        self.assertEqual(coverage["record_count"], 4)
        self.assertEqual([row["member_id"] for row in coverage["members"]], [10, 20])
        self.assertIn("before the SQL query's member/status filters and LIMIT", coverage["scope"])

    def test_aggregates_ctes_and_quoted_tables_keep_source_coverage(self):
        self.seed()
        for sql in (
            'SELECT COUNT(*) AS n FROM "records"."leadership_records" WHERE member_id=999',
            "WITH cases AS (SELECT * FROM records.leadership_records WHERE status='active') "
            "SELECT COUNT(*) AS n FROM cases",
        ):
            with self.subTest(sql=sql):
                coverage = self.coverage(self.query(sql)[0])
                self.assertEqual((coverage["record_count"], coverage["member_count"]), (6, 4))

    def test_query_and_coverage_limits_never_reduce_total_counts(self):
        self.seed()
        for suffix in ("", " LIMIT 1"):
            with self.subTest(suffix=suffix):
                result, _ = self.query(
                    "SELECT id FROM records.leadership_records ORDER BY id" + suffix,
                    max_rows=1,
                )
                coverage = self.coverage(result)
                self.assertEqual(result["row_count"], 1)
                self.assertEqual((coverage["record_count"], coverage["member_count"]), (6, 4))
                self.assertEqual(len(coverage["members"]), 1)
                self.assertTrue(coverage["members_truncated"])

    def test_empty_and_one_sided_windows_are_explicit(self):
        self.seed()
        for params, count, bounds in (
            ({"records_after_ts": 201}, 0, {"after_ts": 201, "before_ts": None}),
            ({"records_after_ts": 200}, 1, {"after_ts": 200, "before_ts": None}),
            ({"records_before_ts": 100}, 1, {"after_ts": None, "before_ts": 100}),
        ):
            with self.subTest(params=params):
                coverage = self.coverage(self.query(
                    "SELECT COUNT(*) AS n FROM records.leadership_records", params,
                )[0])
                self.assertEqual(coverage["record_count"], count)
                self.assertEqual(coverage["window"], bounds)

    def test_invalid_window_is_refused_instead_of_guessing_coverage(self):
        for params in (
            {"records_after_ts": True}, {"records_after_ts": "100"},
            {"records_before_ts": -1}, {"records_before_ts": 1.5},
            {"records_after_ts": 200, "records_before_ts": 100},
            {"records_after_ts": 100, "records_before_ts": 100},
        ):
            with self.subTest(params=params):
                result, touched = self.query("SELECT * FROM records.leadership_records", params)
                self.assertIn("UTC Unix seconds", result["error"])
                self.assertNotIn("source_coverage", result)
                self.assertFalse(touched)

    def test_unrelated_and_denied_queries_do_not_disclose_records(self):
        self.seed()
        result, _ = self.query("SELECT 'records.leadership_records' AS text", levels=set())
        self.assertNotIn("source_coverage", result)
        result, touched = self.query("SELECT * FROM records.leadership_records", levels=set())
        self.assertEqual(len(result["rows"]), 6)
        self.coverage(result)
        self.assertFalse(touched)

    async def test_agent_query_retains_coverage_and_rechecks_access(self):
        context = SimpleNamespace(private_pass=True,
            bot=SimpleNamespace(paths=self.paths), guild=object(), member=SimpleNamespace(id=1),
            state=AgentTurnState(),
        )
        with (
            patch("elbow_helper.agent.datasets.catalogue.SOURCES", self.sources),
            patch("elbow_helper.agent.datasets.query.require_evidence_access", AsyncMock()),
            patch("elbow_helper.agent.datasets.query.has_reader_access_requirements",
                  side_effect=lambda guild, member, levels: levels <= {"lead_plus"}),
        ):
            result = await query_bot_data(context, {
                "sql": "SELECT id FROM records.leadership_records "
                "WHERE created_ts >= :records_after_ts AND created_ts < :records_before_ts",
                "params": self.window,
            })
        self.coverage(model_result(result))
        self.assertEqual(context.state.required_access, set())

    def test_capability_catalogue_explains_optional_source_and_coverage(self):
        from elbow_helper.agent.engine.registry import build_agent_tools

        definition = dataset_tools()[0].definition
        self.assertIn("Read records only for records questions or specific logged cases", definition.usage_note)
        self.assertIn("Records are optional, often empty; absence is not evidence", definition.description)
        hint = build_agent_tools()["query_bot_data"].returns
        self.assertIn("source_coverage?", hint)
        self.assertIn("record_count,member_count,members[]", hint)
