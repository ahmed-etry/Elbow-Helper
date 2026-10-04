"""Status-only, source-authorized hibernation tools."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import uuid4

from elbow_helper.configuration.channels import HIBERNATION_LOG
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...engine.capability_contract import CapabilityContract
from ...access import accessible_message_channel, require_evidence_access
from ...reports.base import ArtifactCapacityError, retain_report
from .report import HibernationReport
from ...models import AgentRequestContext, RegisteredAgentTool


TOOL_CONTRACTS = {
    'read_active_hibernation': CapabilityContract(
        entity_fields=(),
        time_fields=(),
        result_paths=(('report_id',), ('records', 'N', 'member_id')),
        result_path_kinds=((('report_id',), 'hibernation_report'),
                           (('records', 'N', 'member_id'), 'discord_member')),
    ),
    'read_active_hibernation_report': CapabilityContract(
        entity_fields=(('report_id', 'hibernation_report'), ('member_id', 'discord_member')),
        time_fields=(),
        retained_fields=('report_id',),
        result_paths=(('report_id',), ('records', 'N', 'member_id')),
        result_path_kinds=((('report_id',), 'hibernation_report'),
                           (('records', 'N', 'member_id'), 'discord_member')),
    ),
}

def hibernation_tools() -> tuple[RegisteredAgentTool, ...]:
    definitions = (
        ("read_active_hibernation",
         "Read status-only active hibernation records when the requester and bot can access the hibernation log. Returns member IDs and recorded start times only, 25 records unless limit asks for more (up to 500, enough to pass every member ID to a later step); saved roles and private ticket content are excluded. This does not change hibernation.",
         {"limit": {"type": "integer", "minimum": 1, "maximum": 500}}, (), read_active_hibernation),
        ("read_active_hibernation_report",
         "Read another page or exact-member filter from a retained status-only hibernation report without rereading private state.",
         {"report_id": {"type": "string", "maxLength": 32},
          "offset": {"type": "integer", "minimum": 0},
          "limit": {"type": "integer", "minimum": 1, "maximum": 500},
          "member_id": {"type": "integer", "minimum": 1}},
         ("report_id",), read_active_hibernation_report),
    )
    return tuple(RegisteredAgentTool(AgentToolDefinition(
        name=name, description=description,
        parameters={"type": "object", "properties": properties,
                    "required": list(required), "additionalProperties": False},
    ), handler,
        contract=TOOL_CONTRACTS[name],
    ) for name, description, properties, required, handler in definitions)


async def read_active_hibernation(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    limit = arguments.get("limit", 25)
    if type(limit) is not int or not 1 <= limit <= 500:
        return {"error": "Use a limit from 1 to 500."}
    await require_evidence_access(context)
    if context.hibernation_queries is None:
        return {"error": "Hibernation status evidence is not available."}
    if await accessible_message_channel(context, HIBERNATION_LOG) is None:
        return {"error": "Hibernation status evidence is not accessible from this conversation."}
    try:
        snapshot = context.hibernation_queries.active_snapshot()
    except ValueError:
        return {"error": "Stored hibernation status could not be read."}
    context.state.source_channels.add(HIBERNATION_LOG)
    await require_evidence_access(context)
    try:
        report = HibernationReport(
            uuid4().hex, context.guild.id, HIBERNATION_LOG, snapshot,
        )
    except ValueError:
        return {"error": "Stored hibernation status could not be read."}
    if not report.snapshot.records:
        return {**report.manifest(), "report_id": None}
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {"error": "The complete hibernation status report is too large to retain in this conversation."}
    return report.page(limit=limit)


async def read_active_hibernation_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if not isinstance(report, HibernationReport) or report.guild_id != context.guild.id:
        return {"error": "That hibernation status report is not available in this conversation. Read the status again."}
    member_id = arguments.get("member_id")
    if member_id is not None and (type(member_id) is not int or member_id <= 0):
        return {"error": "A valid Discord member ID is required."}
    result = report.page(
        offset=arguments.get("offset", 0), limit=arguments.get("limit", 25),
        member_id=member_id,
    )
    await require_evidence_access(context)
    return result


__all__ = ["hibernation_tools"]
