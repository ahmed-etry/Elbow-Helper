"""Tool adapter for bounded, request-shaped spreadsheets."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..engine.capability_contract import CapabilityContract
from ..access import require_evidence_access
from ..datasets.query import query_context
from .workbooks import publish_workbook_bytes, render_workbook_bytes
from .report_tables import materialize_report_table
from ..models import AgentAttachment, AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from .spreadsheets import (
    AgentSpreadsheet, AgentSpreadsheetSheet, valid_sheet_name, valid_text,
    parse_agent_spreadsheet,
)


MAX_DATA_BACKED_SHEETS = 4
MAX_DATA_BACKED_CELLS = 100_000
MAX_DATA_BACKED_CHARACTERS = 1_000_000


def spreadsheet_tools() -> tuple[RegisteredAgentTool, ...]:
    cell = {"type": "string", "minLength": 0, "maxLength": 2_000}
    heading = {"type": "string", "minLength": 1, "maxLength": 2_000}
    source_column = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "field": {"type": "string", "minLength": 1, "maxLength": 100},
            "heading": heading,
        },
        "required": ["field", "heading"],
    }
    columns = {"type": "array", "minItems": 1, "maxItems": 20, "uniqueItems": True}

    def sheet(properties, required):
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 31}, **properties,
            },
            "required": ["name", *required],
        }

    sheets = {"anyOf": [
        sheet({
            "columns": {**columns, "items": heading},
            "rows": {
                "type": "array", "maxItems": 100,
                "items": {"type": "array", "minItems": 1, "maxItems": 20, "items": cell},
            },
        }, ["columns", "rows"]),
        sheet({
            "sql": {"type": "string", "minLength": 1, "maxLength": 4000},
            "params": {"type": "object", "additionalProperties": True},
        }, ["sql"]),
        sheet({
            "report_id": {"type": "string", "minLength": 1, "maxLength": 32},
            "collection": {"type": "string", "minLength": 1, "maxLength": 100},
            "sheet_name": {"type": "string", "minLength": 1, "maxLength": 31},
            "columns": {**columns, "items": source_column},
        }, ["report_id", "collection", "columns"]),
        sheet({
            "rows_from": {
                "type": "array", "items": {"type": "object", "additionalProperties": True},
                "x-result-list": True,
                "description": (
                    "Reference {step, path} to an earlier step's list of records, "
                    "also accepted inside a one-item list."
                ),
            },
            "columns": {**columns, "items": source_column},
        }, ["rows_from", "columns"]),
    ]}
    return (RegisteredAgentTool(
        AgentToolDefinition(
            name="prepare_spreadsheet",
            description=(
                "Prepare a spreadsheet with Google Sheet and Download buttons "
                "or an XLSX attachment. Use authorized evidence and clearly "
                "labelled recommendations or assumptions. Choose a "
                "descriptive title, sheets, columns and literal text rows that fit "
                "the request. Put tables that belong together into one spreadsheet "
                "as separate sheets; make separate spreadsheets only when the asker "
                "wants separate files. Query sheets use sql and params; saved report sheets "
                "use report_id, collection and field/heading columns. Optional "
                "sheet_name selects an imported XLSX sheet, whose fields are "
                "zero-based column indexes written as strings. Result sheets use "
                "rows_from referencing an earlier step's records and field/heading "
                "columns, with dotted fields for nested values. Never truncate "
                "evidence to fit this tool. Use google_sheet_published to describe "
                "delivery; do not paste "
                "or invent spreadsheet links."
            ),
            parameters={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "title": {
                        "type": "string", "minLength": 1, "maxLength": 80,
                    },
                    "sheets": {
                        "type": "array", "minItems": 1, "maxItems": 4,
                        "items": sheets,
                    },
                },
                "required": ["title", "sheets"],
            },
        ),
        prepare_spreadsheet,
        AgentCapabilityEffect.ARTIFACT,
        contract=CapabilityContract(entity_fields=(), source_scope="request_context"),
    ),)


async def prepare_spreadsheet(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    specifications = arguments.get("sheets")
    if (
        not isinstance(specifications, list)
        or not 1 <= len(specifications) <= MAX_DATA_BACKED_SHEETS
        or not valid_text(arguments.get("title"), maximum=80)
    ):
        return {"error": "The spreadsheet title or sheets are invalid"}
    sheets = []
    try:
        for specification in specifications:
            if (
                not isinstance(specification, dict)
                or not valid_sheet_name(specification.get("name"))
            ):
                raise ValueError("A spreadsheet sheet is invalid")
            kinds = {"sql", "report_id", "rows", "rows_from"}.intersection(specification)
            if len(kinds) != 1:
                raise ValueError("Choose exactly one kind per sheet.")
            if "rows_from" in specification:
                if set(specification) != {"name", "rows_from", "columns"}:
                    raise ValueError("Choose exactly one kind per sheet.")
                sheets.append(_result_sheet(specification))
            elif "report_id" in specification:
                if set(specification) - {
                    "name", "report_id", "collection", "sheet_name", "columns",
                }:
                    raise ValueError("Choose exactly one kind per sheet.")
                sheets.append(await _report_sheet(context, specification))
            elif "sql" in specification:
                if set(specification) - {"name", "sql", "params"}:
                    raise ValueError("Choose exactly one kind per sheet.")
                result = await query_context(
                    context, specification["sql"], specification.get("params"), max_rows=None,
                )
                if "error" in result:
                    return result
                columns = tuple(result["rows"][0]) if result["rows"] else tuple(result["columns"])
                rows = tuple(
                    tuple("" if row[column] is None else str(row[column]) for column in columns)
                    for row in result["rows"]
                )
                sheets.append(AgentSpreadsheetSheet(specification["name"], columns, rows))
            else:
                sheets.extend(parse_agent_spreadsheet({
                    "title": arguments["title"], "sheets": [specification],
                }).sheets)
        written = [item for item in specifications if "rows" in item]
        if written:
            parse_agent_spreadsheet({"title": arguments["title"], "sheets": written})
        if len({sheet.name.casefold() for sheet in sheets}) != len(sheets):
            raise ValueError("A spreadsheet sheet name is repeated")
        cells = sum(len(sheet.columns) * (len(sheet.rows) + 1) for sheet in sheets)
        characters = sum(
            len(str(value)) for sheet in sheets for row in (sheet.columns, *sheet.rows)
            for value in row
        )
        has_reports = any("report_id" in item for item in specifications)
        if has_reports:
            characters += sum(len(sheet.name) for sheet in sheets)
        if cells > MAX_DATA_BACKED_CELLS or characters > MAX_DATA_BACKED_CHARACTERS:
            if has_reports:
                raise ValueError("The complete report spreadsheet exceeds its export limit")
            return {"error": "The export is too large; narrow the query."}
        spreadsheet = AgentSpreadsheet(arguments["title"], tuple(sheets))
    except (KeyError, TypeError, ValueError) as error:
        return {"error": str(error)}
    if has_reports:
        await require_evidence_access(context)
    return await _store_spreadsheet(context, spreadsheet)


async def _report_sheet(context, specification) -> AgentSpreadsheetSheet:
    """Materialize a complete retained table with its recorded authorization."""
    name = specification.get("name")
    report_id = specification.get("report_id")
    collection = specification.get("collection")
    source_sheet = specification.get("sheet_name")
    columns = specification.get("columns")
    if (
        not valid_sheet_name(name)
        or not isinstance(report_id, str)
        or not isinstance(collection, str)
        or source_sheet is not None and not valid_sheet_name(source_sheet)
        or not isinstance(columns, list)
        or not 1 <= len(columns) <= 20
        or any(
            not isinstance(column, dict)
            or not valid_text(column.get("field"), maximum=100)
            or not valid_text(column.get("heading"))
            for column in columns
        )
        or len({column["heading"].casefold() for column in columns})
        != len(columns)
    ):
        raise ValueError("A report sheet is invalid")
    report = context.state.reports.get(report_id)
    sources = context.state.report_sources.get(report_id)
    requirements = context.state.report_access_requirements.get(report_id)
    if (
        report is None or sources is None or requirements is None
        or not sources <= context.state.source_channels
        or not requirements <= context.state.required_access
    ):
        raise ValueError("That report is not authorized in this conversation")
    return await asyncio.to_thread(
        materialize_report_table, report, name=name,
        collection=collection, columns=columns,
        page_options={"sheet_name": source_sheet} if source_sheet else None,
    )


def _result_sheet(specification) -> AgentSpreadsheetSheet:
    records = specification["rows_from"]
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ValueError("rows_from must be a list of objects.")
    columns = specification["columns"]
    if (
        not isinstance(columns, list) or not 1 <= len(columns) <= 20
        or any(
            not isinstance(column, dict) or set(column) != {"field", "heading"}
            or not valid_text(column.get("field"), maximum=100)
            or not valid_text(column.get("heading"))
            for column in columns
        )
        or len({column["heading"].casefold() for column in columns}) != len(columns)
    ):
        raise ValueError("A result sheet's columns are invalid.")

    def value(record, field):
        for part in field.split("."):
            if not isinstance(record, dict):
                return ""
            record = record.get(part)
        if record is None:
            return ""
        return record if type(record) in (int, float) else str(record)

    return AgentSpreadsheetSheet(
        specification["name"], tuple(column["heading"] for column in columns),
        tuple(tuple(value(record, column["field"]) for column in columns) for record in records),
    )


async def _store_spreadsheet(
    context: AgentRequestContext, spreadsheet: AgentSpreadsheet,
) -> Mapping[str, Any]:
    attachment_key = f"spreadsheet:{spreadsheet.content_fingerprint}"
    prepared = next((
        item for item in context.state.attachments
        if item.report_id == attachment_key
    ), None)
    if prepared is not None:
        return _result(prepared, spreadsheet)
    if len(context.state.attachments) >= 4:
        return {"error": "Four report files are already prepared for this reply."}

    filename = spreadsheet.filename
    filenames = {item.filename for item in context.state.attachments}
    if filename in filenames:
        filename = (
            f"{filename[:-5]}-{spreadsheet.content_fingerprint[:8]}.xlsx"
        )
    data = await render_workbook_bytes(
        context.bot, filesize_limit=context.guild.filesize_limit,
        temp_prefix="agent_spreadsheet", sheets=spreadsheet.workbook(),
    )
    await require_evidence_access(context)
    if data is None:
        return {"error": "The spreadsheet exceeds this server's attachment size limit."}
    google_link, google_warning = await publish_workbook_bytes(context.bot, data, spreadsheet.title)
    await require_evidence_access(context)
    prepared = AgentAttachment(
        filename, data, attachment_key, google_link, google_warning, spreadsheet.title,
    )
    context.state.attachments.append(prepared)
    return _result(prepared, spreadsheet)


def _result(prepared: AgentAttachment, spreadsheet: AgentSpreadsheet) -> dict[str, Any]:
    return {
        "filename": prepared.filename, "sheets": len(spreadsheet.sheets),
        "rows": sum(len(sheet.rows) for sheet in spreadsheet.sheets),
        "attachment_prepared": not bool(prepared.google_link),
        "google_sheet_published": bool(prepared.google_link),
    }
