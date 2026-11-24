"""Read-only SQL uses synthetic data and a separate connection per call."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tests.temp_directory import TemporaryDirectory
from types import SimpleNamespace
import unittest
import zlib
from unittest.mock import patch, AsyncMock

from elbow_helper.agent.reads.sql.sources import DataSource
from elbow_helper.agent.reads.sql.query import run_query
from elbow_helper.agent.reads.sql.guide import DataGuide


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

    def test_roster_query_with_more_source_files_than_sqlite_can_attach(self):
        tables = (("rosters", "none", "Synthetic rosters"),)
        sources = []
        for index in range(11):
            alias = f"rosters_{index}"
            path = self.paths.data_root / f"{alias}.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE rosters (id INTEGER)")
                connection.execute("INSERT INTO rosters VALUES (?)", (index,))
            sources.append(DataSource(alias, path, tables=tables))
        self.sources += tuple(sources)

        result, _ = self.query('SELECT id FROM "rosters_10" . [rosters]')
        self.assertEqual(result.get("rows"), [{"id": 10}], result)

    def test_hidden_column_is_absent_from_select_star(self):
        from dataclasses import replace
        self.sources = (replace(self.sources[0], hidden_columns=(("accounts", "name"),)), *self.sources[1:])
        result, _ = self.query("SELECT * FROM first.accounts WHERE id = 1")
        self.assertEqual(result["rows"], [{"id": 1}])
        result, _ = self.query("SELECT id FROM first.accounts WHERE name = 'Synthetic 1'")
        self.assertEqual(result["rows"], [])
        with closing(sqlite3.connect(self.paths.data_root / "second.sqlite3")) as conn:
            conn.execute("UPDATE accounts SET name = NULL WHERE id = 1")
            conn.commit()
        result, _ = self.query(
            "SELECT visible.id, visible.name FROM second.accounts AS visible "
            "JOIN first.accounts AS masked ON masked.id = visible.id "
            "WHERE visible.id = 1 AND masked.name IS NULL"
        )
        self.assertEqual(result["rows"], [{"id": 1, "name": None}])

    def test_iso_dates_use_sql_microseconds_without_hand_conversion(self):
        result, _ = self.query(
            "SELECT unix_us('2026-09-19') AS day, unix_us(:start) AS start, "
            "unix_us('2026-09-19T22:57:36+02:00') AS offset_start, "
            "unix_us('2026-09-19T20:57:36') AS naive_start, "
            "iso_utc(unix_us(:start)) AS iso, "
            "unix_us(iso_utc(-1)) AS before_epoch, "
            "unix_us(iso_utc(1789851456123456)) AS precise, "
            "unix_us(NULL) AS null_us, iso_utc(NULL) AS null_iso",
            {'start': '2026-09-19T20:57:36Z'},
        )
        self.assertEqual(result['rows'], [{
            'day': 1789776000000000, 'start': 1789851456000000,
            'offset_start': 1789851456000000, 'naive_start': 1789851456000000,
            'iso': '2026-09-19T20:57:36.000000+00:00', 'before_epoch': -1,
            'precise': 1789851456123456, 'null_us': None, 'null_iso': None,
        }])

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

    def test_positional_bindings_have_actionable_feedback_then_named_retry_works(self):
        for placeholder in ("?", "@id", "$id"):
            result, touched = self.query(
                f"SELECT name FROM first.accounts WHERE id = {placeholder}", {"1": 3},
            )
            self.assertEqual(result["cause"], "invalid_arguments")
            self.assertEqual(result["invalid_fields"], ["sql", "params"])
            self.assertIn(":id", result["instruction"])
            self.assertFalse(touched)
        result, _ = self.query("SELECT name FROM first.accounts WHERE id = :id", {"id": 3})
        self.assertEqual(result["rows"], [{"name": "Synthetic 3"}])

    def test_placeholder_characters_inside_literals_and_comments_are_not_bindings(self):
        result, _ = self.query("SELECT '? @id $id' AS text FROM first.accounts WHERE id = :id -- ?", {"id": 2})
        self.assertEqual(result["rows"], [{"text": "? @id $id"}])


    def test_denied_table_names_access_level(self):
        result, touched = self.query("SELECT * FROM second.accounts", levels=set())
        self.assertEqual(result, {
            "error": "This needs lead access.", "required_access": ["lead"],
            "cause": "access_denied",
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
        with patch('elbow_helper.agent.reads.sql.query.QUERY_SECONDS', 0.001):
            result, _ = self.query(
                "WITH RECURSIVE numbers(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM numbers) "
                "SELECT sum(n) FROM numbers"
            )
        self.assertIn("took too long", result["error"])
        (self.paths.project_root / "second.sqlite3").unlink()
        self.assertNotIn(
            "second.accounts", DataGuide(self.paths, self.sources).for_levels({"lead"}),
        )


    def test_truncated_count_uses_one_execution_and_export_retains_the_complete_set(self):
        statements = []
        connect = sqlite3.connect

        def traced(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        sql = 'SELECT id FROM first.accounts ORDER BY id'
        with patch('elbow_helper.agent.reads.sql.query.sqlite3.connect', traced):
            result, _ = self.query(sql, max_rows=2)
        self.assertEqual(result['rows'], [{'id': 0}, {'id': 1}])
        self.assertEqual(result['total_rows'], 5)
        self.assertEqual(statements.count(sql), 1)
        self.assertFalse(any('COUNT(*) FROM (' in query for query in statements))
        result, _ = self.query('WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i<1500) SELECT i FROM n', max_rows=None)
        self.assertEqual(result['rows'], [{'i': i} for i in range(1, 1501)])
        self.assertEqual(result['row_count'], 1500)
        self.assertFalse(result['truncated'])

    def test_sqlite_rejects_an_oversized_scalar_before_allocating_it(self):
        from elbow_helper.agent.reads.sql.query import MAX_QUERY_VALUE_BYTES
        result, touched = self.query('SELECT zeroblob(:size) AS value',
                                     {'size': MAX_QUERY_VALUE_BYTES + 1})
        self.assertEqual(result['cause'], 'capacity')
        self.assertFalse(touched)

    def test_interactive_and_export_results_share_the_fetch_memory_bound(self):
        for max_rows in (200, None):
            with self.subTest(max_rows=max_rows), patch(
                'elbow_helper.agent.reads.sql.query.MAX_QUERY_RESULT_BYTES', 64):
                result, touched = self.query('SELECT :value AS text FROM first.accounts',
                                             {'value': 'x' * 100}, max_rows=max_rows)
            self.assertEqual(result['cause'], 'capacity')
            self.assertNotIn('rows', result)
            self.assertFalse(touched)

    def test_result_over_the_size_limit_returns_what_fits_and_says_how_many_rows_were_cut(self):
        with patch('elbow_helper.agent.reads.sql.query.MAX_QUERY_RESULT_BYTES', 250):
            result, _ = self.query('SELECT :value AS text FROM first.accounts', {'value': 'x' * 100})
        self.assertEqual(result['row_count'], 2)
        self.assertTrue(result['truncated'])
        self.assertEqual((result['total_rows'], result['rows_cut']), (5, 3))
        self.assertIn('size limit', result['instruction'])
        with patch('elbow_helper.agent.reads.sql.query.MAX_QUERY_RESULT_BYTES', 250):
            result, _ = self.query('SELECT :value AS text FROM first.accounts', {'value': 'x' * 100},
                                   max_rows=None)
        self.assertEqual(result['cause'], 'capacity')


    def test_empty_columns_and_large_cells(self):
        self.assertEqual(
            self.query("SELECT name FROM first.accounts WHERE id=999")[0]["columns"], ["name"],
        )
        self.assertEqual(
            self.query("SELECT X'FF' AS blob, :text AS text", {"text": "x" * 4001})[0]["rows"],
            [{"blob": "ff", "text": "x" * 4001}],
        )

    def test_sql_decoders_keep_compressed_records_whole_and_refuse_oversized_values(self):
        from elbow_helper.agent.reads.sql.query import MAX_QUERY_VALUE_BYTES

        doc = json.dumps([{'value': 'x' * 5000}])
        with closing(sqlite3.connect(self.paths.project_root / 'first.sqlite3')) as connection:
            connection.execute('ALTER TABLE accounts ADD COLUMN payload BLOB')
            connection.execute('UPDATE accounts SET payload=? WHERE id=0', (zlib.compress(doc.encode()),))
            connection.commit()
        result, _ = self.query('SELECT unz(payload) AS doc FROM first.accounts WHERE id=0')
        self.assertEqual(result['rows'], [{'doc': doc}])
        with closing(sqlite3.connect(self.paths.project_root / 'first.sqlite3')) as connection:
            connection.execute('UPDATE accounts SET payload=? WHERE id=0',
                               (zlib.compress(b'x' * (MAX_QUERY_VALUE_BYTES + 1)),))
            connection.commit()
        result, _ = self.query('SELECT unz(payload) AS doc FROM first.accounts WHERE id=0')
        self.assertIn('too large', result['error'])
        self.assertNotIn('rows', result)
        result, _ = self.query('SELECT army(:code) AS army', {'code': 'u1x0s1x0'})
        self.assertIn('troops', json.loads(result['rows'][0]['army']))
        result, _ = self.query('SELECT army(NULL) AS army, unz(NULL) AS doc')
        self.assertEqual(result['rows'], [{'army': None, 'doc': None}])
        result, _ = self.query('SELECT army(:code) AS army', {'code': 'unreadable-code'})
        self.assertIn('unreadable-code', result['error'])


    def test_quoted_source_refusal_and_duplicate_columns(self):
        self.assertEqual(
            self.query("SELECT * FROM \"SECOND\".\"accounts\"", levels=set())[0]["required_access"],
            ["lead"],
        )
        self.assertIn("error", self.query("SELECT id,id FROM first.accounts")[0])

    async def test_context_retains_and_rechecks_touched_access(self):
        from elbow_helper.agent.reads.sql.query import query_context
        from elbow_helper.agent.engine.models import AgentTurnState

        context = SimpleNamespace(
            bot=SimpleNamespace(paths=self.paths), guild=object(), member=SimpleNamespace(id=1),
            state=AgentTurnState(),
        )
        access = {"lead"}
        with (
            patch('elbow_helper.agent.reads.sql.catalogue.SOURCES', self.sources),
            patch(
                'elbow_helper.agent.reads.sql.query.require_evidence_access', AsyncMock(),
            ),
            patch(
                'elbow_helper.agent.reads.sql.query.has_reader_access_requirements',
                side_effect=lambda guild, member, levels: levels <= access,
            ),
        ):
            result = await query_context(context, "SELECT * FROM second.accounts")
            self.assertNotIn("error", result)
            self.assertEqual(context.state.required_access, {"lead"})
            context.state.required_access.clear()

            async def read_and_revoke(function, *args, **kwargs):
                result = function(*args, **kwargs)
                access.clear()
                return result

            with patch(
                'elbow_helper.agent.reads.sql.query.asyncio.to_thread', read_and_revoke,
            ):
                result = await query_context(context, "SELECT * FROM second.accounts")
            self.assertEqual(result["required_access"], ["lead"])
            self.assertEqual(context.state.required_access, set())
