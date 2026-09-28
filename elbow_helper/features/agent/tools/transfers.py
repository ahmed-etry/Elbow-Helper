"""Permission-filtered read tools for unresolved clan-transfer queues."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import uuid4

from elbow_helper.features.clan_transfers.config import CLAN_TRANSFER_QUEUES
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import accessible_message_channel, require_evidence_access
from ..reports.base import ArtifactCapacityError, retain_report
from ..models import AgentRequestContext, RegisteredAgentTool
from ..reports.transfer import TransferQueueReport


def transfer_tools() -> tuple[RegisteredAgentTool, ...]:
    definitions = (
        ("read_pending_transfer_requests",
         "Read unresolved clan transfer requests from registered queue threads the requester and bot can currently access. An optional clan code narrows the source read to one queue. Requests are destination claims, not approvals or completed transfers; expired stored rows are counted but omitted. This does not change queues.",
         {"clan_code": {"type": "string", "enum": sorted(CLAN_TRANSFER_QUEUES)}},
         (), read_pending_transfer_requests),
        ("read_pending_transfer_report",
         "Read another filtered page from a retained pending transfer report without rereading or changing queue state.",
         {"report_id": {"type": "string", "maxLength": 32},
          "offset": {"type": "integer", "minimum": 0},
          "limit": {"type": "integer", "minimum": 1, "maximum": 25},
          "clan_code": {"type": "string",
                        "description": "Brown Elbow clan code such as BEH or BE4."},
          "member_id": {"type": "integer", "minimum": 1}},
         ("report_id",), read_pending_transfer_report),
    )
    return tuple(RegisteredAgentTool(AgentToolDefinition(
        name=name, description=description,
        parameters={"type": "object", "properties": properties,
                    "required": list(required), "additionalProperties": False},
    ), handler) for name, description, properties, required, handler in definitions)


async def read_pending_transfer_requests(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.transfer_queries is None:
        return {"error": "Clan transfer queue evidence is not available."}
    selected_clan_code = arguments.get("clan_code")
    if selected_clan_code is not None and (
        not isinstance(selected_clan_code, str)
        or selected_clan_code not in CLAN_TRANSFER_QUEUES
    ):
        return {"error": "Unknown Brown Elbow transfer destination."}
    registrations = tuple(
        row for row in context.transfer_queries.queue_registrations()
        if selected_clan_code is None or row.clan_code == selected_clan_code
    )
    accessible = []
    for registration in registrations:
        if await accessible_message_channel(context, registration.thread_id) is None:
            continue
        accessible.append(registration)
    try:
        snapshot = context.transfer_queries.pending_snapshot(
            clan_codes=tuple(row.clan_code for row in accessible),
        )
    except ValueError:
        return {"error": "Stored clan transfer requests could not be read."}
    context.state.source_channels.update(row.thread_id for row in accessible)
    await require_evidence_access(context)
    try:
        report = TransferQueueReport(
            uuid4().hex, context.guild.id, snapshot.observed_at,
            snapshot.request_ttl_hours, len(registrations),
            len(registrations) - len(accessible), snapshot.queues,
        )
    except ValueError:
        return {"error": "Stored clan transfer requests could not be read."}
    if not any(queue.pending for queue in report.queues):
        return {**report.manifest(), "report_id": None,
                "selected_clan_code": selected_clan_code}
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {"error": "The complete pending transfer report is too large to retain in this conversation."}
    return {**report.page(), "selected_clan_code": selected_clan_code}


async def read_pending_transfer_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if not isinstance(report, TransferQueueReport) or report.guild_id != context.guild.id:
        return {"error": "That pending transfer report is not available in this conversation. Read the queues again."}
    clan_code = None
    if "clan_code" in arguments:
        value = str(arguments.get("clan_code") or "").strip().upper()
        if value not in CLAN_TRANSFER_QUEUES:
            return {
                "error": "Unknown Brown Elbow transfer destination.",
                "known_clan_codes": sorted(CLAN_TRANSFER_QUEUES),
            }
        clan_code = value
    member_id = arguments.get("member_id")
    if member_id is not None and (type(member_id) is not int or member_id <= 0):
        return {"error": "A valid Discord member ID is required."}
    result = report.page(
        offset=arguments.get("offset", 0), limit=arguments.get("limit", 25),
        clan_code=clan_code, member_id=member_id,
    )
    await require_evidence_access(context)
    return result


__all__ = ["transfer_tools"]
