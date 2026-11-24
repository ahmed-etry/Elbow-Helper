"""Earlier result records become complete, typed workbook rows."""

from io import BytesIO
from tests.temp_directory import TemporaryDirectory
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from tests.agent.files.test_agent_spreadsheets import _context
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.files.spreadsheet_tools import (
    prepare_spreadsheet, spreadsheet_tools,
)
from elbow_helper.agent.plan.checker import valid_arguments
from elbow_helper.agent.plan.executor import resolve_arguments


def _arguments(records):
    return {"title": "Synthetic", "sheets": [{
        "name": "Scores", "rows_from": records, "columns": [
            {"field": "account.name", "heading": "Account"},
            {"field": "score", "heading": "Score"},
            {"field": "missing.value", "heading": "Missing"},
        ],
    }]}


class ResultSpreadsheetTests(unittest.IsolatedAsyncioTestCase):

    def test_sheet_kinds_require_their_own_fields_and_column_shapes(self):
        schema = spreadsheet_tools()[0].definition.parameters
        field_columns = [{"field": "value", "heading": "Value"}]
        sheets = [
            {"name": "Written", "rows": [[""]], "columns": ["Value"]},
            {"name": "Query", "sql": "SELECT :value", "params": {"value": 7}},
            {"name": "Report", "report_id": "synthetic", "collection": "items",
             "columns": field_columns, "sheet_name": "Source"},
            {"name": "Result", "rows_from": {"step": "read", "path": ["items"]},
             "columns": field_columns},
        ]
        required_fields = (
            ("name", "rows", "columns"), ("name", "sql"),
            ("name", "report_id", "collection", "columns"), ("name", "rows_from", "columns"),
        )
        for sheet, required_fields in zip(sheets, required_fields):
            with self.subTest(sheet=sheet):
                arguments = {"title": "Synthetic", "sheets": [sheet]}
                self.assertTrue(valid_arguments(arguments, schema, {"read"}))
                for required in required_fields:
                    missing = {key: value for key, value in sheet.items() if key != required}
                    self.assertFalse(valid_arguments(
                        {"title": "Synthetic", "sheets": [missing]}, schema, {"read"},
                    ))
                mixed = {**sheet, "sql" if "sql" not in sheet else "rows": "synthetic"}
                self.assertFalse(valid_arguments(
                    {"title": "Synthetic", "sheets": [mixed]}, schema, {"read"},
                ))
                if "columns" in sheet:
                    wrong = {**sheet, "columns": (field_columns if "rows" in sheet
                                                  else ["Value"])}
                    self.assertFalse(valid_arguments(
                        {"title": "Synthetic", "sheets": [wrong]}, schema, {"read"},
                    ))


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


    async def test_bad_referenced_shape_keeps_specific_error_for_both_reference_forms(self):
        schema = build_agent_tools()["prepare_spreadsheet"].definition.parameters
        reference = {"step": "scores", "path": ["records"]}
        with TemporaryDirectory() as directory:
            context = _context(directory)
            for records in (None, {}, {"score": 7}, "bad", [1], [[{}]]):
                for source in (reference, [reference]):
                    with self.subTest(records=records, source=source):
                        arguments = resolve_arguments(
                            _arguments(source), {"scores": {"records": records}}, schema=schema,
                        )
                        self.assertTrue(valid_arguments(arguments, schema, {"scores"}))
                        result = await prepare_spreadsheet(context, arguments)
                        self.assertEqual(result, {
                            "error": "rows_from must be a list of objects.",
                        })
            self.assertEqual(context.state.attachments, [])

    async def test_result_sheets_enforce_query_caps_without_truncation(self):
        with TemporaryDirectory() as directory:
            for constant, cap in (("MAX_DATA_BACKED_CELLS", 8),
                                  ("MAX_DATA_BACKED_CHARACTERS", 30)):
                context = _context(directory)
                with self.subTest(constant=constant), patch(
                    "elbow_helper.agent.files.spreadsheet_tools." + constant, cap,
                ):
                    result = await prepare_spreadsheet(context, _arguments([
                        {"account": {"name": "Synthetic"}, "score": 7} for _ in range(3)
                    ]))
                    self.assertEqual(result, {
                        "error": "The export is too large; narrow the query.",
                    })
                    self.assertEqual(context.state.attachments, [])
