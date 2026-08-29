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
