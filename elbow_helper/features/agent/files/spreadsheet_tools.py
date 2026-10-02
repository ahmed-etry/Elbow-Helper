"""Tool adapter for bounded, request-shaped spreadsheets."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..engine.capability_contract import CapabilityContract
from ..access import require_evidence_access
from .workbooks import render_workbook_bytes
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
    cell = {"type": "string", "maxLength": 2_000}
    heading = {"type": "string", "minLength": 1, "maxLength": 2_000}
    sheet = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 31},
            "columns": {
                "type": "array", "minItems": 1, "maxItems": 20,
                "uniqueItems": True, "items": heading,
            },
            "rows": {
                "type": "array", "maxItems": 100,
                "items": {
                    "type": "array", "minItems": 1, "maxItems": 20,
                    "items": cell,
                },
            },
        },
        "required": ["name", "columns", "rows"],
    }
    source_column = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "field": {"type": "string", "minLength": 1, "maxLength": 100},
            "heading": heading,
        },
        "required": ["field", "heading"],
    }
    report_sheet = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 31},
            "report_id": {"type": "string", "minLength": 1, "maxLength": 32},
            "collection": {"type": "string", "minLength": 1, "maxLength": 100},
            "sheet_name": {"type": "string", "minLength": 1, "maxLength": 31},
            "columns": {"type": "array", "minItems": 1, "maxItems": 20,
                        "items": source_column},
        },
        "required": ["name", "report_id", "collection", "columns"],
    }
    authored_sheet = {**sheet, "properties": dict(sheet["properties"])}
    return (RegisteredAgentTool(
        AgentToolDefinition(
            name="prepare_spreadsheet",
            description=(
                "Prepare an XLSX requested by the asker from authorized evidence "
                "and clearly labelled recommendations or assumptions. Choose a "
                "descriptive title, sheets, columns and literal text rows that fit "
                "the request. Never truncate evidence to fit this tool."
            ),
            parameters={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "title": {
                        "type": "string", "minLength": 1, "maxLength": 80,
                    },
                    "sheets": {
                        "type": "array", "minItems": 1, "maxItems": 4,
                        "items": sheet,
                    },
                },
                "required": ["title", "sheets"],
            },
        ),
        prepare_spreadsheet,
        AgentCapabilityEffect.ARTIFACT,
        contract=CapabilityContract(
            entity_fields=(),
            time_fields=(),
            source_scope="request_context",
        ),
            ), RegisteredAgentTool(
        AgentToolDefinition(
            name="prepare_report_spreadsheet",
            description=(
                "Prepare one XLSX from complete retained report tables without "
                "repeating rows in tool arguments. Choose each report, paginated "
                "collection, field, and heading from authorized evidence. Optional "
                "sheet_name selects an imported XLSX sheet; imported data fields "
                "use zero-based column indexes written as strings. Optional "
                "written sheets can hold request-specific findings or proposals. "
                "The export fails rather than silently dropping rows or columns."
            ),
            parameters={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 80},
                    "report_sheets": {"type": "array", "minItems": 1,
                                      "maxItems": MAX_DATA_BACKED_SHEETS,
                                      "items": report_sheet},
                    "written_sheets": {"type": "array", "maxItems": 3,
                                       "items": authored_sheet},
                },
                "required": ["title", "report_sheets"],
            },
        ),
        prepare_report_spreadsheet,
        AgentCapabilityEffect.ARTIFACT,
        contract=CapabilityContract(
            entity_fields=(),
            time_fields=(),
            source_scope="request_context",
        ),
       ))


async def prepare_spreadsheet(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    try:
        spreadsheet = parse_agent_spreadsheet(arguments)
    except ValueError as error:
        return {"error": str(error)}
    return await _store_spreadsheet(context, spreadsheet)


async def prepare_report_spreadsheet(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Render complete report tables plus optional model-authored summary rows."""
    await require_evidence_access(context)
    raw_report_sheets = arguments.get("report_sheets")
    raw_written = arguments.get("written_sheets", [])
    title = arguments.get("title")
    if (
        not valid_text(title, maximum=80)
        or not isinstance(raw_report_sheets, list)
        or not 1 <= len(raw_report_sheets) <= MAX_DATA_BACKED_SHEETS
        or not isinstance(raw_written, list)
        or len(raw_report_sheets) + len(raw_written) > MAX_DATA_BACKED_SHEETS
    ):
        return {"error": "The report spreadsheet layout is invalid."}
    try:
        written = (
            parse_agent_spreadsheet({"title": title, "sheets": raw_written}).sheets
            if raw_written else ()
        )
        sheets: list[AgentSpreadsheetSheet] = list(written)
        for specification in raw_report_sheets:
            if not isinstance(specification, dict):
                raise ValueError("A report sheet is invalid")
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
            sheets.append(await asyncio.to_thread(
                materialize_report_table, report, name=name,
                collection=collection, columns=columns,
                page_options={"sheet_name": source_sheet} if source_sheet else None,
            ))
        if len({sheet.name.casefold() for sheet in sheets}) != len(sheets):
            raise ValueError("Spreadsheet sheet names must be unique")
        cells = sum(len(sheet.columns) * (len(sheet.rows) + 1) for sheet in sheets)
        characters = sum(
            len(sheet.name) + sum(map(len, sheet.columns))
            + sum(sum(map(len, row)) for row in sheet.rows)
            for sheet in sheets
        )
        if cells > MAX_DATA_BACKED_CELLS or characters > MAX_DATA_BACKED_CHARACTERS:
            raise ValueError("The complete report spreadsheet exceeds its export limit")
        spreadsheet = AgentSpreadsheet(title, tuple(sheets))
    except (KeyError, TypeError, ValueError) as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    return await _store_spreadsheet(context, spreadsheet)


async def _store_spreadsheet(
    context: AgentRequestContext, spreadsheet: AgentSpreadsheet,
) -> Mapping[str, Any]:
    attachment_key = f"spreadsheet:{spreadsheet.content_fingerprint}"
    prepared = next((
        item for item in context.state.attachments
        if item.report_id == attachment_key
    ), None)
    if prepared is not None:
        return _result(prepared.filename, spreadsheet)
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
    context.state.attachments.append(AgentAttachment(
        filename, data, attachment_key,
    ))
    return _result(filename, spreadsheet)


def _result(filename: str, spreadsheet: AgentSpreadsheet) -> dict[str, Any]:
    return {
        "filename": filename, "sheets": len(spreadsheet.sheets),
        "rows": sum(len(sheet.rows) for sheet in spreadsheet.sheets),
        "attachment_prepared": True,
    }
