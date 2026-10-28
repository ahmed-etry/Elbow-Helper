"""Reconstruct typed report data within its guild boundary."""

from typing import Any
from ..files.contracts import (
    CsvImportArtifact,
    TextImportArtifact,
    XlsxImportArtifact,
    XlsxSheet,
)


def decode_attachment(row: dict[str, Any], guild_id: int) -> Any:
    return decode_attachment_report(row, guild_id=guild_id)


def decode_attachment_report(row: dict[str, Any], *, guild_id: int) -> Any:
    common = {
        "report_id": row["report_id"], "guild_id": row["guild_id"],
        "channel_id": row["channel_id"], "message_id": row["message_id"],
        "attachment_id": row["attachment_id"], "filename": row["filename"],
        "content_type": row["content_type"], "byte_size": row["byte_size"],
        "sha256": row["sha256"], "imported_at": row["imported_at"],
        "parser_version": row["parser_version"],
    }
    if row["kind"] == "csv_import":
        report = CsvImportArtifact(
            **common, encoding=row["encoding"], delimiter=row["delimiter"],
            columns=tuple(row["columns"]),
            rows=tuple(tuple(item) for item in row["rows"]),
            issues=tuple(row["issues"]),
        )
    elif row["kind"] == "xlsx_import":
        report = XlsxImportArtifact(
            **common, uncompressed_size=row["uncompressed_size"],
            sheets=tuple(XlsxSheet(
                item["name"], tuple(item["columns"]),
                tuple(tuple(value) for value in item["rows"]),
                tuple(item["issues"]),
            ) for item in row["sheets"]),
        )
    else:
        report = TextImportArtifact(
            **common, encoding=row["encoding"],
            document_format=row["document_format"], text=row["text"],
            line_count=row["line_count"],
        )
    if report.guild_id != guild_id:
        label = {
            "csv_import": "CSV import",
            "xlsx_import": "XLSX import",
            "text_import": "Text import",
        }[row["kind"]]
        raise ValueError(f"{label} belongs to another guild")
    return report
