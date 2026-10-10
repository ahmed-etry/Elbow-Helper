from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import os
import unittest
from unittest.mock import MagicMock
from unittest.mock import patch
import zipfile

from elbow_helper.infrastructure.exports import GoogleSheetsPublisher
from elbow_helper.infrastructure.exports import LocalExportStore
from elbow_helper.infrastructure.exports import WorkbookWriter
from elbow_helper.infrastructure.exports.google_sheets import GOOGLE_EXPORT_OWNER_KEY
from elbow_helper.infrastructure.exports.google_sheets import GOOGLE_EXPORT_OWNER_VALUE
from elbow_helper.infrastructure.exports.google_sheets import MAX_WORKBOOK_COLUMN_PIXELS


class WorkbookWriterTests(unittest.TestCase):
    def test_writer_creates_a_valid_multi_sheet_archive(self) -> None:
        with TemporaryDirectory() as directory:
            workbook_path = Path(directory) / "export.xlsx"

            WorkbookWriter().write(
                workbook_path,
                [
                    ("Overview", [["Name", "Score"], ["Ahmad", 2.75]]),
                    ("Overview", [["Status"], ["Ready"]]),
                ],
            )

            with zipfile.ZipFile(workbook_path) as workbook:
                workbook_xml = workbook.read("xl/workbook.xml").decode()
                first_sheet = workbook.read(
                    "xl/worksheets/sheet1.xml"
                ).decode()

        self.assertIn('name="Overview"', workbook_xml)
        self.assertIn('name="Overview_2"', workbook_xml)
        self.assertIn("<v>2.75</v>", first_sheet)
        self.assertIn('state="frozen"', first_sheet)


class GoogleSheetsPublisherTests(unittest.TestCase):
    def test_workbook_upload_uses_the_central_folder(self) -> None:
        drive = MagicMock()
        drive.files.return_value.create.return_value.execute.return_value = {
            "id": "sheet-id",
            "webViewLink": "https://docs.google.com/spreadsheets/d/sheet-id/edit",
        }
        drive.files.return_value.list.return_value.execute.return_value = {}
        media_upload = MagicMock()
        publisher = GoogleSheetsPublisher(
            client_id="client",
            client_secret="secret",
            refresh_token="refresh",
            folder_id="https://drive.google.com/drive/folders/folder-id",
        )

        with (
            patch("google.oauth2.credentials.Credentials"),
            patch("google.auth.transport.requests.Request"),
            patch("googleapiclient.discovery.build", return_value=drive),
            patch(
                "googleapiclient.http.MediaFileUpload",
                return_value=media_upload,
            ),
        ):
            link, warning = publisher.upload_workbook_sync(
                Path("report.xlsx"),
                "Report",
            )

        self.assertEqual(
            link,
            "https://docs.google.com/spreadsheets/d/sheet-id/edit",
        )
        self.assertIsNone(warning)
        create_call = drive.files.return_value.create.call_args.kwargs
        self.assertEqual(create_call["body"]["parents"], ["folder-id"])
        self.assertEqual(
            create_call["body"]["appProperties"],
            {GOOGLE_EXPORT_OWNER_KEY: GOOGLE_EXPORT_OWNER_VALUE},
        )
        self.assertIs(create_call["media_body"], media_upload)
        cleanup_query = drive.files.return_value.list.call_args.kwargs["q"]
        self.assertIn("appProperties has", cleanup_query)
        self.assertIn(GOOGLE_EXPORT_OWNER_VALUE, cleanup_query)
        self.assertIn("'folder-id' in parents", cleanup_query)

    def test_converted_workbook_fits_every_sheet_then_clamps_only_wide_columns(self) -> None:
        drive = MagicMock()
        drive.files.return_value.create.return_value.execute.return_value = {"id": "synthetic"}
        drive.files.return_value.list.return_value.execute.return_value = {}
        sheets = MagicMock()
        api = sheets.spreadsheets.return_value
        api.get.return_value.execute.side_effect = [
            {"sheets": [{"properties": {
                "sheetId": sheet_id, "gridProperties": {"columnCount": count, "rowCount": rows},
            }} for sheet_id, count, rows in ((7, 3, 17), (19, 2, 1))]},
            {"sheets": [
                {"properties": {"sheetId": 7}, "data": [{"columnMetadata": [
                    {"pixelSize": 180}, {"pixelSize": 425}, {"pixelSize": 900},
                ]}]},
                {"properties": {"sheetId": 19}, "data": [{
                    "startColumn": 1, "columnMetadata": [{"pixelSize": 600}],
                }]},
            ]},
        ]
        publisher = GoogleSheetsPublisher(
            client_id="synthetic", client_secret="synthetic", refresh_token="synthetic",
            folder_id=None,
        )
        with (
            patch("google.oauth2.credentials.Credentials"),
            patch("google.auth.transport.requests.Request"),
            patch("googleapiclient.discovery.build", side_effect=[drive, sheets]) as build,
            patch("googleapiclient.http.MediaFileUpload"),
        ):
            link, warning = publisher.upload_workbook_sync(Path("synthetic.xlsx"), "Synthetic")
        self.assertEqual(link, "https://docs.google.com/spreadsheets/d/synthetic/edit")
        self.assertIsNone(warning)
        self.assertEqual([call.args[:2] for call in build.call_args_list],
                         [("drive", "v3"), ("sheets", "v4")])
        self.assertEqual(api.batchUpdate.call_args_list[0].kwargs, {
            "spreadsheetId": "synthetic", "body": {"requests": [
                {"autoResizeDimensions": {"dimensions": {
                    "sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0,
                    "endIndex": count,
                }}} for sheet_id, count in ((7, 3), (19, 2))
            ]},
        })
        self.assertEqual(MAX_WORKBOOK_COLUMN_PIXELS, 425)
        self.assertEqual(api.batchUpdate.call_args_list[1].kwargs, {
            "spreadsheetId": "synthetic", "body": {"requests": [
                {"updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                              "startIndex": index, "endIndex": index + 1},
                    "properties": {"pixelSize": 425}, "fields": "pixelSize",
                }} for sheet_id, index in ((7, 2), (19, 1))
            ] + [{"autoResizeDimensions": {"dimensions": {
                "sheetId": 7, "dimension": "ROWS", "startIndex": 1, "endIndex": 17,
            }}}]},
        })
        self.assertEqual([call[0] for call in api.method_calls],
                         ["get", "batchUpdate", "get", "batchUpdate"])
        self.assertIn("columnMetadata(pixelSize)", api.get.call_args.kwargs["fields"])

    def test_failed_column_fit_still_returns_the_created_sheet(self) -> None:
        drive = MagicMock()
        drive.files.return_value.create.return_value.execute.return_value = {"id": "synthetic"}
        drive.files.return_value.list.return_value.execute.return_value = {}
        sheets = MagicMock()
        sheets.spreadsheets.return_value.get.return_value.execute.side_effect = ValueError("synthetic")
        publisher = GoogleSheetsPublisher(
            client_id="synthetic", client_secret="synthetic", refresh_token="synthetic",
            folder_id=None,
        )
        with (
            patch("google.oauth2.credentials.Credentials"),
            patch("google.auth.transport.requests.Request"),
            patch("googleapiclient.discovery.build", side_effect=[drive, sheets]),
            patch("googleapiclient.http.MediaFileUpload"),
            self.assertLogs("elbow_helper.infrastructure.exports.google_sheets", "WARNING"),
        ):
            link, warning = publisher.upload_workbook_sync(Path("synthetic.xlsx"), "Synthetic")
        self.assertEqual(link, "https://docs.google.com/spreadsheets/d/synthetic/edit")
        self.assertIsNone(warning)

    def test_fitted_columns_within_the_cap_need_no_clamp_request(self) -> None:
        sheets = MagicMock()
        api = sheets.spreadsheets.return_value
        api.get.return_value.execute.side_effect = [
            {"sheets": [{"properties": {
                "sheetId": 7, "gridProperties": {"columnCount": 2},
            }}]},
            {"sheets": [{"properties": {"sheetId": 7}, "data": [{"columnMetadata": [
                {}, {"pixelSize": MAX_WORKBOOK_COLUMN_PIXELS},
            ]}]}]},
        ]
        GoogleSheetsPublisher._fit_workbook_columns(sheets, "synthetic")
        api.batchUpdate.assert_called_once()

    def test_cleanup_deletes_only_files_selected_by_the_managed_export_query(self) -> None:
        drive = MagicMock()
        drive.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "expired-managed-sheet"}],
        }

        deleted = GoogleSheetsPublisher._cleanup_exports(
            drive,
            folder_id="folder-id",
        )

        self.assertEqual(deleted, 1)
        query = drive.files.return_value.list.call_args.kwargs["q"]
        self.assertIn("appProperties has", query)
        self.assertIn(GOOGLE_EXPORT_OWNER_VALUE, query)
        self.assertIn("createdTime <", query)
        drive.files.return_value.delete.assert_called_once_with(
            fileId="expired-managed-sheet",
            supportsAllDrives=True,
        )

    def test_missing_oauth_settings_are_reported_without_google_io(self) -> None:
        publisher = GoogleSheetsPublisher(
            client_id=None,
            client_secret=None,
            refresh_token=None,
            folder_id=None,
        )

        link, warning = publisher.upload_workbook_sync(
            Path("report.xlsx"),
            "Report",
        )

        self.assertIsNone(link)
        self.assertEqual(warning, "Google Sheets hasn't been set up.")


class LocalExportStoreTests(unittest.TestCase):
    def test_temporary_paths_are_unique_and_delete_stays_inside_store(self) -> None:
        with TemporaryDirectory() as directory:
            store = LocalExportStore(Path(directory))
            first = store.temporary_path("Roster Export")
            second = store.temporary_path("Roster Export")
            first.write_bytes(b"first")
            outside = Path(directory).parent / "outside-export.xlsx"

            self.assertNotEqual(first, second)
            self.assertIsNone(store.delete(first))
            self.assertFalse(first.exists())
            self.assertIn("refused", store.delete(outside) or "")

    def test_cleanup_only_removes_expired_matching_files(self) -> None:
        with TemporaryDirectory() as directory:
            store = LocalExportStore(Path(directory), retention_days=1)
            expired = store.path_for("expired.xlsx")
            current = store.path_for("current.xlsx")
            other = store.path_for("note.txt")
            for path in (expired, current, other):
                path.write_bytes(b"data")
            old = (
                datetime.now(timezone.utc) - timedelta(days=2)
            ).timestamp()
            os.utime(expired, (old, old))
            os.utime(other, (old, old))

            deleted, warning = store.cleanup("*.xlsx")

            self.assertEqual(deleted, 1)
            self.assertIsNone(warning)
            self.assertFalse(expired.exists())
            self.assertTrue(current.exists())
            self.assertTrue(other.exists())


if __name__ == "__main__":
    unittest.main()
