"""Materialize complete, model-selected tables from retained report pages."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from .spreadsheets import AgentSpreadsheetSheet, valid_text


REPORT_PAGE_SIZE = 25
MAX_REPORT_TABLE_ROWS = 10_000
MAX_REPORT_TABLE_CELLS = 100_000


def materialize_report_table(
    report: Any, *, name: str, collection: str,
    columns: Sequence[Mapping[str, str]],
    page_options: Mapping[str, Any] | None = None,
) -> AgentSpreadsheetSheet:
    """Follow a report's pagination cursor without model-echoing its rows."""
    page_reader = getattr(report, "page", None)
    if not callable(page_reader):
        raise ValueError("That report cannot be exported as a table")
    fields = tuple(column["field"] for column in columns)
    headings = tuple(column["heading"] for column in columns)
    if len(set(fields)) != len(fields):
        raise ValueError("A report column was selected more than once")

    rows: list[tuple[str, ...]] = []
    offset = 0
    while True:
        try:
            page = page_reader(
                offset=offset, limit=REPORT_PAGE_SIZE, **dict(page_options or {}),
            )
        except (TypeError, ValueError, KeyError) as error:
            raise ValueError("That report does not support complete table export") from error
        if not isinstance(page, Mapping) or "next_offset" not in page:
            raise ValueError("That report does not expose a complete table cursor")
        batch = page.get(collection)
        if not isinstance(batch, list):
            raise ValueError("That report collection is not a table")
        source_columns = page.get("columns") if collection == "data" else None
        if source_columns is not None:
            if (
                not isinstance(source_columns, list)
                or any(not isinstance(field, str) for field in source_columns)
                or any(not field.isascii() or not field.isdecimal() for field in fields)
                or len({int(field) for field in fields}) != len(fields)
                or any(int(field) >= len(source_columns) for field in fields)
                or any(
                    not isinstance(row, list) or len(row) != len(source_columns)
                    for row in batch
                )
            ):
                raise ValueError("A selected imported-file column is invalid")
            selected_rows = (
                tuple(_cell(row[int(field)]) for field in fields)
                for row in batch
            )
        else:
            if any(not isinstance(row, Mapping) for row in batch):
                raise ValueError("That report collection is not a record table")
            if any(any(field not in row for field in fields) for row in batch):
                raise ValueError("A selected column is absent from the report table")
            selected_rows = (
                tuple(_cell(row[field]) for field in fields)
                for row in batch
            )
        if len(rows) + len(batch) > MAX_REPORT_TABLE_ROWS:
            raise ValueError("The report table exceeds the complete export row limit")
        if (len(rows) + len(batch) + 1) * len(fields) > MAX_REPORT_TABLE_CELLS:
            raise ValueError("The report table exceeds the complete export cell limit")
        rows.extend(selected_rows)
        next_offset = page["next_offset"]
        if next_offset is None:
            break
        if type(next_offset) is not int or next_offset != offset + len(batch) or not batch:
            raise ValueError("That collection does not follow the report's table cursor")
        offset = next_offset

    return AgentSpreadsheetSheet(name, headings, tuple(rows))


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        rendered = json.dumps(value, ensure_ascii=False, default=str)
    else:
        rendered = str(value)
    if not valid_text(rendered, allow_empty=True):
        raise ValueError("A report value cannot fit in a spreadsheet cell")
    return rendered
