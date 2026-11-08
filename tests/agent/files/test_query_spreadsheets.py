"""SQL exports keep every selected row or fail their cap."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import closing
from io import BytesIO
from pathlib import Path
import sqlite3
from tests.temp_directory import TemporaryDirectory

from openpyxl import load_workbook

from agent.files.test_agent_spreadsheets import _context
from elbow_helper.agent.tool_support.sql.sources import DataSource
from elbow_helper.agent.files.spreadsheet_tools import prepare_spreadsheet


class QuerySpreadsheetTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_and_written_sheets_render_complete_literal_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / "accounts.sqlite3")) as connection:
                connection.execute("CREATE TABLE accounts (id INTEGER, note TEXT)")
                connection.executemany(
                    "INSERT INTO accounts VALUES (?,?)", [(i, "=1+1") for i in range(150)],
                )
                connection.commit()
            context = _context(directory)
            context.bot.paths = SimpleNamespace(project_root=root, data_root=root)
            sources = (DataSource(
                "accounts", Path("accounts.sqlite3"),
                tables=(("accounts", "none", "Synthetic accounts"),),
            ),)
            with patch('elbow_helper.agent.tool_support.sql.catalogue.SOURCES', sources):
                result = await prepare_spreadsheet(context, {"title": "Synthetic", "sheets": [
                    {
                        "name": "Evidence",
                        "sql": "SELECT id,note FROM accounts.accounts ORDER BY id",
                    },
                    {"name": "Notes", "columns": ["Finding"], "rows": [["Synthetic finding"]]},
                ]})
            self.assertEqual(result["rows"], 151)
            workbook = load_workbook(BytesIO(context.state.attachments[0].data))
            try:
                self.assertEqual(workbook["Evidence"].max_row, 151)
                self.assertEqual(workbook["Evidence"]["A151"].value, "149")
                self.assertEqual(workbook["Evidence"]["B151"].data_type, "s")
                self.assertEqual(workbook["Notes"]["A2"].value, "Synthetic finding")
            finally:
                workbook.close()
