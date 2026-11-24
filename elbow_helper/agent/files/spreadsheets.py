"""Validated, literal-value workbooks shaped by the agent request."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
import unicodedata
from typing import Any, Mapping, Sequence
from elbow_helper.agent.conversation.wording import SPREADSHEET_FILENAME_STEM
from elbow_helper.domain.player_tags import canonical_player_tag


MAX_SPREADSHEET_SHEETS = 10
MAX_SPREADSHEET_COLUMNS = 20
MAX_SPREADSHEET_ROWS_PER_SHEET = 100
MAX_SPREADSHEET_CELLS = 8_000
MAX_SPREADSHEET_CELL_CHARACTERS = 2_000
MAX_SPREADSHEET_CHARACTERS = 100_000
SOURCE_CELL_FORMATS = ("tag", "yes_no", "iso_date", "unix_seconds", "unix_microseconds")


def valid_source_columns(columns: Any) -> bool:
    return (
        isinstance(columns, list)
        and 1 <= len(columns) <= MAX_SPREADSHEET_COLUMNS
        and all(
            isinstance(column, dict)
            and column.keys() <= {"field", "heading", "format"}
            and valid_text(column.get("field"), maximum=100)
            and valid_text(column.get("heading"))
            and ("format" not in column or column["format"] in SOURCE_CELL_FORMATS)
            for column in columns
        )
        and len({column["heading"].casefold() for column in columns}) == len(columns)
    )


def source_cell(value: Any, cell_format: str | None) -> Any:
    if value is None or value == "":
        return ""
    try:
        if cell_format == "tag":
            return canonical_player_tag(value) or ""
        if cell_format == "yes_no":
            if value in (0, 1):
                return "Yes" if value else "No"
            raise ValueError("A spreadsheet row does not match its columns")
        if cell_format in ("unix_seconds", "unix_microseconds"):
            epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
            offset = (timedelta(seconds=value) if cell_format == "unix_seconds"
                      else timedelta(microseconds=value))
            return (epoch + offset).date().isoformat()
        if cell_format == "iso_date" and not isinstance(value, date):
            value = datetime.fromisoformat(value)
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("A spreadsheet row does not match its columns") from error
    return value


@dataclass(frozen=True, slots=True)
class AgentSpreadsheetSheet:
    name: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True, slots=True)
class AgentSpreadsheet:
    title: str
    sheets: tuple[AgentSpreadsheetSheet, ...]

    @property
    def content_fingerprint(self) -> str:
        payload = {
            "title": self.title,
            "sheets": [
                {"name": sheet.name, "columns": sheet.columns,
                 "rows": sheet.rows}
                for sheet in self.sheets
            ],
        }
        return hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    @property
    def filename(self) -> str:
        normalized = unicodedata.normalize("NFKD", self.title)
        slug = re.sub(
            r"-+", "-", "".join(
                character.casefold() if character.isascii() and character.isalnum()
                else "-" for character in normalized
            ),
        ).strip("-")[:60].rstrip("-")
        return f"{slug or SPREADSHEET_FILENAME_STEM}.xlsx"

    def workbook(self) -> tuple[tuple[str, Sequence[Sequence[Any]]], ...]:
        return tuple(
            (sheet.name, (sheet.columns, *sheet.rows)) for sheet in self.sheets
        )


def parse_agent_spreadsheet(arguments: Mapping[str, Any]) -> AgentSpreadsheet:
    """Validate untrusted model-authored tabular output before packaging it."""
    title = arguments.get("title")
    raw_sheets = arguments.get("sheets")
    if not valid_text(title, maximum=80) or not isinstance(raw_sheets, list):
        raise ValueError("The spreadsheet title or sheets are invalid")
    if not 1 <= len(raw_sheets) <= MAX_SPREADSHEET_SHEETS:
        raise ValueError("The spreadsheet must contain between one and ten sheets")

    sheets = []
    total_cells = 0
    total_characters = len(title)
    for raw_sheet in raw_sheets:
        if not isinstance(raw_sheet, dict) or set(raw_sheet) != {
            "name", "columns", "rows",
        }:
            raise ValueError("A spreadsheet sheet is invalid")
        name = raw_sheet["name"]
        columns = raw_sheet["columns"]
        rows = raw_sheet["rows"]
        if (
            not valid_sheet_name(name)
            or not isinstance(columns, list)
            or not 1 <= len(columns) <= MAX_SPREADSHEET_COLUMNS
            or any(not valid_text(value) for value in columns)
            or len({value.casefold() for value in columns}) != len(columns)
            or not isinstance(rows, list)
            or len(rows) > MAX_SPREADSHEET_ROWS_PER_SHEET
        ):
            raise ValueError("A spreadsheet table is invalid")
        parsed_rows = []
        for row in rows:
            if (
                not isinstance(row, list) or len(row) != len(columns)
                or any(not valid_text(value, allow_empty=True) for value in row)
            ):
                raise ValueError("A spreadsheet row does not match its columns")
            parsed_rows.append(tuple(row))
            total_characters += sum(len(value) for value in row)
        total_cells += len(columns) * (len(parsed_rows) + 1)
        total_characters += len(name) + sum(len(value) for value in columns)
        sheets.append(AgentSpreadsheetSheet(
            name, tuple(columns), tuple(parsed_rows),
        ))

    if len({sheet.name.casefold() for sheet in sheets}) != len(sheets):
        raise ValueError("Spreadsheet sheet names must be unique")
    if (
        total_cells > MAX_SPREADSHEET_CELLS
        or total_characters > MAX_SPREADSHEET_CHARACTERS
    ):
        raise ValueError("The spreadsheet exceeds the ad-hoc report limit")
    return AgentSpreadsheet(title, tuple(sheets))


def valid_sheet_name(value: Any) -> bool:
    return (
        valid_text(value, maximum=31)
        and not any(character in value for character in "[]*:/\\?")
        and value[0] != "'" and value[-1] != "'"
    )


def valid_text(
    value: Any, *, maximum: int = MAX_SPREADSHEET_CELL_CHARACTERS,
    allow_empty: bool = False,
) -> bool:
    return bool(
        isinstance(value, str)
        and (allow_empty or bool(value.strip()))
        and len(value) <= maximum
        and not any(
            character not in "\t\n\r"
            and unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in value
        )
    )
