"""Earlier result records become complete, typed workbook rows."""

from io import BytesIO
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from features.agent.files.test_agent_spreadsheets import _context
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.files.spreadsheet_tools import prepare_spreadsheet
from elbow_helper.features.agent.plan import capability_list


def _arguments(records):
    return {"title": "Synthetic", "sheets": [{
        "name": "Scores", "rows_from": records, "columns": [
            {"field": "account.name", "heading": "Account"},
            {"field": "score", "heading": "Score"},
            {"field": "missing.value", "heading": "Missing"},
        ],
    }]}


class ResultSpreadsheetTests(unittest.IsolatedAsyncioTestCase):
    def test_catalogue_shows_result_sheet_fields(self):
        catalogue = capability_list(build_agent_tools())
        self.assertIn("rows_from:[{}]", catalogue)
        self.assertIn("columns:[string/{field:string,heading:string}]", catalogue)

    async def test_nested_missing_and_numeric_values_are_written_exactly(self):
        records = [{"account": {"name": f"Synthetic {i}"}, "score": i + 0.125}
                   for i in range(150)]
        records[1]["account"] = None
        records[2]["score"] = 7
        with TemporaryDirectory() as directory:
            context = _context(directory)
            result = await prepare_spreadsheet(context, _arguments(records))
            self.assertEqual(result["rows"], 150)
            workbook = load_workbook(BytesIO(context.state.attachments[0].data))
            try:
                sheet = workbook["Scores"]
                self.assertEqual(sheet["A2"].value, "Synthetic 0")
                self.assertEqual(sheet["A3"].value, "")
                self.assertEqual(sheet["B2"].value, 0.125)
                self.assertEqual(sheet["B4"].value, 7)
                self.assertEqual(sheet["B151"].value, 149.125)
                self.assertEqual(sheet["B2"].data_type, "n")
                self.assertEqual(sheet["C2"].value, "")
            finally:
                workbook.close()

    async def test_bad_record_shape_returns_specific_error(self):
        with TemporaryDirectory() as directory:
            context = _context(directory)
            for records in (None, {}, "bad", [1], [{}, []]):
                with self.subTest(records=records):
                    result = await prepare_spreadsheet(context, _arguments(records))
                    self.assertEqual(result, {"error": "rows_from must be a list of objects."})
            self.assertEqual(context.state.attachments, [])

    async def test_result_sheets_enforce_query_caps_without_truncation(self):
        with TemporaryDirectory() as directory:
            for constant, cap in (("MAX_DATA_BACKED_CELLS", 8),
                                  ("MAX_DATA_BACKED_CHARACTERS", 30)):
                context = _context(directory)
                with self.subTest(constant=constant), patch(
                    "elbow_helper.features.agent.files.spreadsheet_tools." + constant, cap,
                ):
                    result = await prepare_spreadsheet(context, _arguments([
                        {"account": {"name": "Synthetic"}, "score": 7} for _ in range(3)
                    ]))
                    self.assertEqual(result, {
                        "error": "The export is too large; narrow the query.",
                    })
                    self.assertEqual(context.state.attachments, [])
