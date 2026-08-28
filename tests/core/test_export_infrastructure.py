from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock
from unittest.mock import patch
import zipfile

from elbow_helper.infrastructure.exports import GoogleSheetsPublisher
from elbow_helper.infrastructure.exports import WorkbookWriter


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
                cleanup_name_contains="report_",
                retention_days=0,
            )

        self.assertEqual(
            link,
            "https://docs.google.com/spreadsheets/d/sheet-id/edit",
        )
        self.assertIsNone(warning)
        create_call = drive.files.return_value.create.call_args.kwargs
        self.assertEqual(create_call["body"]["parents"], ["folder-id"])
        self.assertIs(create_call["media_body"], media_upload)

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
            cleanup_name_contains="report_",
            retention_days=0,
        )

        self.assertIsNone(link)
        self.assertEqual(warning, "Google Sheets hasn't been set up.")


if __name__ == "__main__":
    unittest.main()
