"""Read-only SQL uses synthetic data and a separate connection per call."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock

from elbow_helper.features.agent.datasets.sources import DataSource
from elbow_helper.features.agent.datasets.query import run_query
from elbow_helper.features.agent.datasets.guide import DataGuide


class QueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.paths = SimpleNamespace(project_root=root, data_root=root)
        for name in ("first", "second"):
            with closing(sqlite3.connect(root / (name + ".sqlite3"))) as conn:
                conn.executescript(
                    "CREATE TABLE accounts (id INTEGER, name TEXT); "
                    "CREATE TABLE hidden (secret TEXT);"
                )
                conn.executemany(
                    "INSERT INTO accounts VALUES (?,?)",
                    [(i, f"Synthetic {i}") for i in range(5)],
                )
                conn.commit()
        (root / "state.json").write_text(json.dumps({"ids": [1, 3]}))
        self.sources = (
            DataSource(
                "first", Path("first.sqlite3"),
                tables=(("accounts", "none", "Synthetic accounts"),),
            ),
            DataSource(
                "second", Path("second.sqlite3"),
                tables=(("accounts", "lead", "Synthetic accounts"),),
            ),
            DataSource(
                "state", Path("state.json"), state_name="selection", note="{ids:[int]}",
            ),
        )

    def query(self, sql, params=None, max_rows=200, levels=frozenset({"lead"})):
        return run_query(self.sources, self.paths, levels, sql, params, max_rows)

    def test_join_cte_window_and_list_parameter(self):
        result, touched = self.query(
            "WITH joined AS (SELECT a.id,b.name FROM first.accounts a "
            "JOIN second.accounts b ON a.id=b.id) "
            "SELECT *,row_number() OVER (ORDER BY id) AS position FROM joined "
            "WHERE id IN (SELECT value FROM json_each(:ids))",
            {"ids": [1, 3]},
        )
        self.assertNotIn("error", result)
        self.assertEqual([row["id"] for row in result["rows"]], [1, 3])
        self.assertEqual(touched, {"lead"})

    def test_state_json_and_truncation(self):
        result, _ = self.query(
            "SELECT value FROM state.selection,json_each(json_extract(doc,'$.ids'))", max_rows=1,
        )
        self.assertEqual(result["total_rows"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["rows"], [{"value": 1}])

    def test_denied_table_names_access_level(self):
        result, touched = self.query("SELECT * FROM second.accounts", levels=set())
        self.assertEqual(result, {
            "error": "This needs lead access.", "required_access": ["lead"],
        })
        self.assertFalse(touched)

    def test_mutations_metadata_and_extensions_are_refused(self):
        for sql in (
            "INSERT INTO first.accounts VALUES (8,'x')",
            "UPDATE first.accounts SET name='x'",
            "DELETE FROM first.accounts", "CREATE TABLE x(a)", "DROP TABLE first.accounts",
            "ATTACH DATABASE ':memory:' AS extra", "PRAGMA first.table_info(accounts)",
            "SELECT load_extension('x')", "SELECT * FROM first.sqlite_master",
            "SELECT * FROM first.hidden", "SELECT ? FROM first.accounts",
        ):
            with self.subTest(sql=sql):
                self.assertIn("error", self.query(sql)[0])
        self.assertEqual(
            self.query("SELECT COUNT(*) AS n FROM first.accounts")[0]["rows"], [{"n": 5}],
        )

    def test_deadline_and_missing_source(self):
        with patch("elbow_helper.features.agent.datasets.query.QUERY_SECONDS", 0.001):
            result, _ = self.query(
                "WITH RECURSIVE numbers(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM numbers) "
                "SELECT sum(n) FROM numbers"
            )
        self.assertIn("took too long", result["error"])
        (self.paths.project_root / "second.sqlite3").unlink()
        self.assertNotIn(
            "second.accounts", DataGuide(self.paths, self.sources).for_levels({"lead"}),
        )

    def test_bounded_recursive_cte_returns_rows(self):
        result, touched = self.query(
            "WITH RECURSIVE numbers(n) AS ("
            "VALUES(1) UNION ALL SELECT n+1 FROM numbers WHERE n<4"
            ") SELECT n FROM numbers"
        )
        self.assertEqual(result["rows"], [{"n": n} for n in range(1, 5)])
        self.assertFalse(touched)

    def test_empty_columns_and_large_cells(self):
        self.assertEqual(
            self.query("SELECT name FROM first.accounts WHERE id=999")[0]["columns"], ["name"],
        )
        self.assertEqual(
            self.query("SELECT X'FF' AS blob, :text AS text", {"text": "x" * 4001})[0]["rows"],
            [{"blob": "ff", "text": "x" * 4000 + "\u2026"}],
        )

    def test_quoted_source_refusal_and_duplicate_columns(self):
        self.assertEqual(
            self.query("SELECT * FROM \"SECOND\".\"accounts\"", levels=set())[0]["required_access"],
            ["lead"],
        )
        self.assertIn("error", self.query("SELECT id,id FROM first.accounts")[0])

    async def test_context_retains_and_rechecks_touched_access(self):
        from elbow_helper.features.agent.datasets.query import query_context
        from elbow_helper.features.agent.models import AgentTurnState

        context = SimpleNamespace(
            bot=SimpleNamespace(paths=self.paths), guild=object(), member=SimpleNamespace(id=1),
            state=AgentTurnState(),
        )
        access = {"lead"}
        with (
            patch("elbow_helper.features.agent.datasets.catalogue.SOURCES", self.sources),
            patch(
                "elbow_helper.features.agent.datasets.query.require_evidence_access", AsyncMock(),
            ),
            patch(
                "elbow_helper.features.agent.datasets.query.has_access_requirements",
                side_effect=lambda guild, member, levels: levels <= access,
            ),
        ):
            result = await query_context(context, "SELECT * FROM second.accounts")
            self.assertNotIn("error", result)
            self.assertEqual(context.state.required_access, {"lead"})
            context.state.required_access.clear()

            async def read_and_revoke(function, *args):
                result = function(*args)
                access.clear()
                return result

            with patch(
                "elbow_helper.features.agent.datasets.query.asyncio.to_thread", read_and_revoke,
            ):
                result = await query_context(context, "SELECT * FROM second.accounts")
            self.assertEqual(result["required_access"], ["lead"])
            self.assertEqual(context.state.required_access, set())
