"""Tool adapter for bounded, request-shaped spreadsheets."""

from __future__ import annotations

from typing import Any, Mapping

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..files.delivery import render_workbook_bytes
from ..models import AgentAttachment, AgentRequestContext, RegisteredAgentTool
from ..files.spreadsheets import AgentSpreadsheet, parse_agent_spreadsheet


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
    ),)


async def prepare_spreadsheet(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    try:
        spreadsheet = parse_agent_spreadsheet(arguments)
    except ValueError as error:
        return {"error": str(error)}
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
