"""Google Drive and Sheets publishing behind one application-owned service."""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import logging
from pathlib import Path
import re
from typing import Any
from typing import Sequence

from .models import ExportSheet


LOGGER = logging.getLogger(__name__)
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
)
SPREADSHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
XLSX_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
GOOGLE_EXPORT_RETENTION_DAYS = 30
GOOGLE_EXPORT_OWNER_KEY = "managed_by"
GOOGLE_EXPORT_OWNER_VALUE = "elbow-helper"
HEADER_FILL = "374151"
HEADER_HEIGHT_PX = 44
ZEBRA_FILL = "E6E6E6"


def _folder_id(value: str | None) -> str:
    cleaned = str(value or "").strip()
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", cleaned)
    return match.group(1) if match else cleaned


def _google_color(hex_value: str) -> dict[str, float]:
    value = hex_value.strip().lstrip("#")
    return {
        "red": int(value[0:2], 16) / 255.0,
        "green": int(value[2:4], 16) / 255.0,
        "blue": int(value[4:6], 16) / 255.0,
    }


def _google_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, (int, float)):
        return {"numberValue": value}
    return {"stringValue": "" if value is None else str(value)}


class GoogleSheetsPublisher:
    """Publish generated workbooks or structured tabs with shared OAuth setup."""

    def __init__(
        self,
        *,
        client_id: str | None,
        client_secret: str | None,
        refresh_token: str | None,
        folder_id: str | None,
    ) -> None:
        self._client_id = str(client_id or "").strip()
        self._client_secret = str(client_secret or "").strip()
        self._refresh_token = str(refresh_token or "").strip()
        self.folder_id = _folder_id(folder_id)

    def _credentials(self, user_credentials: Any) -> Any | None:
        if not (
            self._client_id
            and self._client_secret
            and self._refresh_token
        ):
            return None
        return user_credentials.Credentials(
            token=None,
            refresh_token=self._refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self._client_id,
            client_secret=self._client_secret,
            scopes=list(GOOGLE_SCOPES),
        )

    @staticmethod
    def _cleanup_exports(
        drive: Any,
        *,
        folder_id: str,
    ) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=GOOGLE_EXPORT_RETENTION_DAYS
        )
        query_parts = [
            (
                "appProperties has { "
                f"key='{GOOGLE_EXPORT_OWNER_KEY}' and "
                f"value='{GOOGLE_EXPORT_OWNER_VALUE}'"
                " }"
            ),
            "trashed = false",
            f"mimeType = '{SPREADSHEET_MIME_TYPE}'",
            f"createdTime < '{cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}'",
        ]
        if folder_id:
            query_parts.append(f"'{folder_id}' in parents")
        deleted = 0
        page_token = None
        while True:
            listing = drive.files().list(
                q=" and ".join(query_parts),
                fields="nextPageToken,files(id)",
                pageSize=100,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            for item in listing.get("files", []):
                file_id = item.get("id")
                if not file_id:
                    continue
                drive.files().delete(
                    fileId=file_id,
                    supportsAllDrives=True,
                ).execute()
                deleted += 1
            page_token = listing.get("nextPageToken")
            if not page_token:
                return deleted

    def upload_workbook_sync(
        self,
        workbook_path: Path,
        sheet_title: str,
    ) -> tuple[str | None, str | None]:
        """Upload and convert an XLSX workbook into a new Google spreadsheet."""

        try:
            from google.auth.exceptions import GoogleAuthError
            from google.auth.exceptions import RefreshError
            from google.auth.transport.requests import Request
            from google.oauth2 import credentials as user_credentials
            from googleapiclient.discovery import build
            from googleapiclient.errors import HttpError
            from googleapiclient.http import MediaFileUpload
        except ImportError:
            return None, "Google Sheets isn't available here."

        credentials = self._credentials(user_credentials)
        if credentials is None:
            return None, "Google Sheets hasn't been set up."
        try:
            credentials.refresh(Request())
            drive = build(
                "drive",
                "v3",
                credentials=credentials,
                cache_discovery=False,
            )
            metadata: dict[str, Any] = {
                "name": sheet_title,
                "mimeType": SPREADSHEET_MIME_TYPE,
                "appProperties": {
                    GOOGLE_EXPORT_OWNER_KEY: GOOGLE_EXPORT_OWNER_VALUE,
                },
            }
            if self.folder_id:
                metadata["parents"] = [self.folder_id]
            created = drive.files().create(
                body=metadata,
                media_body=MediaFileUpload(
                    str(workbook_path),
                    mimetype=XLSX_MIME_TYPE,
                    resumable=False,
                ),
                fields="id,webViewLink",
                supportsAllDrives=True,
            ).execute()
            file_id = created.get("id")
            if not file_id:
                return None, "I couldn't get a link for the new Google Sheet."
            try:
                deleted = self._cleanup_exports(
                    drive,
                    folder_id=self.folder_id,
                )
                if deleted:
                    LOGGER.info("Deleted %s old Google export files", deleted)
            except (HttpError, RuntimeError, TypeError, ValueError) as error:
                LOGGER.warning("Google export cleanup failed: %s", error)
            return (
                created.get("webViewLink")
                or f"https://docs.google.com/spreadsheets/d/{file_id}/edit",
                None,
            )
        except (RefreshError, GoogleAuthError):
            return None, "Couldn't connect to Google Sheets."
        except (HttpError, OSError, RuntimeError, TypeError, ValueError) as error:
            if "storageQuotaExceeded" in str(error):
                return None, "Google Drive is full."
            return None, "Couldn't create the Google Sheet."

    async def upload_workbook(
        self,
        workbook_path: Path,
        sheet_title: str,
    ) -> tuple[str | None, str | None]:
        return await asyncio.to_thread(
            self.upload_workbook_sync,
            workbook_path,
            sheet_title,
        )

    def create_spreadsheet_sync(
        self,
        *,
        sheets: Sequence[ExportSheet],
        sheet_title: str,
    ) -> tuple[str | None, str | None]:
        """Create a new formatted Google spreadsheet."""

        try:
            from google.auth.exceptions import GoogleAuthError
            from google.auth.exceptions import RefreshError
            from google.auth.transport.requests import Request
            from google.oauth2 import credentials as user_credentials
            from googleapiclient.discovery import build
            from googleapiclient.errors import HttpError
        except ImportError:
            return None, "Google Sheets isn't available here."

        credentials = self._credentials(user_credentials)
        if credentials is None:
            return None, "Google Sheets hasn't been set up."
        target_id = ""
        spreadsheet_url = ""
        drive = None
        try:
            credentials.refresh(Request())
            sheets_api = build(
                "sheets",
                "v4",
                credentials=credentials,
                cache_discovery=False,
            )
            drive = build(
                "drive",
                "v3",
                credentials=credentials,
                cache_discovery=False,
            )
            definitions = [
                {
                    "properties": {
                        "sheetId": sheet_id,
                        "index": sheet_id,
                        "title": sheet.title,
                        "tabColor": _google_color(sheet.tab_color),
                        "gridProperties": {
                            "rowCount": max(len(sheet.rows) + 1, 25),
                            "columnCount": max(len(sheet.columns), 15),
                            "frozenRowCount": 1,
                        },
                    }
                }
                for sheet_id, sheet in enumerate(sheets)
            ]
            created = sheets_api.spreadsheets().create(
                body={
                    "properties": {"title": sheet_title},
                    "sheets": definitions,
                },
                fields="spreadsheetId,spreadsheetUrl",
            ).execute()
            target_id = str(created.get("spreadsheetId") or "")
            spreadsheet_url = str(created.get("spreadsheetUrl") or "")
            if not target_id:
                return None, "I couldn't get a link for the new Google Sheet."
            drive_update: dict[str, Any] = {
                "fileId": target_id,
                "body": {
                    "appProperties": {
                        GOOGLE_EXPORT_OWNER_KEY: GOOGLE_EXPORT_OWNER_VALUE,
                    }
                },
                "fields": "id,parents,appProperties",
                "supportsAllDrives": True,
            }
            if self.folder_id:
                parents = drive.files().get(
                    fileId=target_id,
                    fields="parents",
                    supportsAllDrives=True,
                ).execute().get("parents", [])
                drive_update["addParents"] = self.folder_id
                drive_update["removeParents"] = ",".join(parents)
            drive.files().update(**drive_update).execute()
            bindings = list(enumerate(sheets))
            requests: list[dict[str, Any]] = []

            header_fill = _google_color(HEADER_FILL)
            zebra_fill = _google_color(ZEBRA_FILL)
            for sheet_id, sheet in bindings:
                header_cells = []
                for column in sheet.columns:
                    cell: dict[str, Any] = {
                        "userEnteredValue": {"stringValue": column.name},
                        "userEnteredFormat": {
                            "backgroundColor": header_fill,
                            "horizontalAlignment": "CENTER",
                            "verticalAlignment": "MIDDLE",
                            "wrapStrategy": "WRAP",
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": _google_color("FFFFFF"),
                            },
                        },
                    }
                    if column.note:
                        cell["note"] = column.note
                    header_cells.append(cell)
                row_data: list[dict[str, Any]] = [{"values": header_cells}]
                for row in sheet.rows:
                    values = []
                    for column_index, column in enumerate(sheet.columns):
                        value = (
                            row[column_index]
                            if column_index < len(row)
                            else ""
                        )
                        cell_format: dict[str, Any] = {
                            "horizontalAlignment": column.align.upper(),
                            "verticalAlignment": "TOP",
                            "wrapStrategy": "WRAP",
                        }
                        if isinstance(value, float):
                            cell_format["numberFormat"] = {
                                "type": "NUMBER",
                                "pattern": "0.00",
                            }
                        values.append(
                            {
                                "userEnteredValue": _google_value(value),
                                "userEnteredFormat": cell_format,
                            }
                        )
                    row_data.append({"values": values})
                requests.append(
                    {
                        "updateCells": {
                            "start": {
                                "sheetId": sheet_id,
                                "rowIndex": 0,
                                "columnIndex": 0,
                            },
                            "rows": row_data,
                            "fields": (
                                "userEnteredValue,userEnteredFormat,note"
                            ),
                        }
                    }
                )
                requests.append(
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "ROWS",
                                "startIndex": 0,
                                "endIndex": 1,
                            },
                            "properties": {"pixelSize": HEADER_HEIGHT_PX},
                            "fields": "pixelSize",
                        }
                    }
                )
                for column_index, column in enumerate(sheet.columns):
                    requests.append(
                        {
                            "updateDimensionProperties": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "dimension": "COLUMNS",
                                    "startIndex": column_index,
                                    "endIndex": column_index + 1,
                                },
                                "properties": {
                                    "pixelSize": column.width_px
                                },
                                "fields": "pixelSize",
                            }
                        }
                    )
                if not sheet.rows:
                    continue
                data_range = {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": len(sheet.rows) + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(sheet.columns),
                }
                requests.extend(
                    [
                        {
                            "addConditionalFormatRule": {
                                "index": 0,
                                "rule": {
                                    "ranges": [data_range],
                                    "booleanRule": {
                                        "condition": {
                                            "type": "CUSTOM_FORMULA",
                                            "values": [
                                                {
                                                    "userEnteredValue": (
                                                        "=MOD(ROW(),2)=0"
                                                    )
                                                }
                                            ],
                                        },
                                        "format": {
                                            "backgroundColor": zebra_fill
                                        },
                                    },
                                },
                            }
                        },
                        {
                            "setBasicFilter": {
                                "filter": {
                                    "range": {
                                        **data_range,
                                        "startRowIndex": 0,
                                    }
                                }
                            }
                        },
                    ]
                )
                for column_index, choices in sheet.dropdowns:
                    requests.append(
                        {
                            "setDataValidation": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "startRowIndex": 1,
                                    "endRowIndex": len(sheet.rows) + 1,
                                    "startColumnIndex": column_index,
                                    "endColumnIndex": column_index + 1,
                                },
                                "rule": {
                                    "condition": {
                                        "type": "ONE_OF_LIST",
                                        "values": [
                                            {"userEnteredValue": choice}
                                            for choice in choices
                                        ],
                                    },
                                    "strict": True,
                                    "showCustomUi": True,
                                },
                            }
                        }
                    )

            sheets_api.spreadsheets().batchUpdate(
                spreadsheetId=target_id,
                body={"requests": requests},
            ).execute()
            try:
                self._cleanup_exports(
                    drive,
                    folder_id=self.folder_id,
                )
            except (HttpError, RuntimeError, TypeError, ValueError) as error:
                LOGGER.warning("Google export cleanup failed: %s", error)
            return (
                spreadsheet_url
                or f"https://docs.google.com/spreadsheets/d/{target_id}/edit",
                None,
            )
        except (RefreshError, GoogleAuthError):
            return None, "Couldn't connect to Google Sheets."
        except (HttpError, OSError, RuntimeError, TypeError, ValueError):
            if target_id and drive is not None:
                try:
                    drive.files().delete(
                        fileId=target_id,
                        supportsAllDrives=True,
                    ).execute()
                except Exception:
                    pass
            return None, "Couldn't create the Google Sheet."

    async def create_spreadsheet(
        self,
        *,
        sheets: Sequence[ExportSheet],
        sheet_title: str,
    ) -> tuple[str | None, str | None]:
        return await asyncio.to_thread(
            self.create_spreadsheet_sync,
            sheets=sheets,
            sheet_title=sheet_title,
        )
