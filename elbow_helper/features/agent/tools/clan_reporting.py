"""Permission-filtered Missing Elder evidence adapters."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import uuid4

from elbow_helper.configuration.channels import CLAN_LEADERSHIP_CHANNELS
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import accessible_message_channel, require_evidence_access
from ..reports.base import ArtifactCapacityError, retain_report
from ..reports.clan_reporting import MissingElderReport
from ..models import AgentRequestContext, RegisteredAgentTool


def clan_reporting_tools() -> tuple[RegisteredAgentTool, ...]:
    clan_code = {
        "type": "string", "enum": list(CLAN_LEADERSHIP_CHANNELS),
    }
    definitions = (
        (
            "read_missing_elder_accounts",
            "Read and retain the existing Missing Elder calculation for selected family clans whose leadership channels the requester and bot can access. It uses current linked-account, Discord-role and cached in-game-role evidence; inaccessible clans and malformed rows are explicit. This does not refresh data, change roles or update boards.",
            {
                "clan_codes": {
                    "type": "array", "items": clan_code,
                    "minItems": 1, "maxItems": len(CLAN_LEADERSHIP_CHANNELS),
                    "uniqueItems": True,
                },
                "clan_code": clan_code,
                "member_id": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            (), read_missing_elder_accounts,
        ),
        (
            "read_missing_elder_report",
            "Read another filtered page from one retained Missing Elder snapshot. Current access to every included clan leadership channel is rechecked; member roles, links and cached clan evidence are not recalculated.",
            {
                "report_id": {
                    "type": "string", "minLength": 1, "maxLength": 32,
                },
                "clan_code": clan_code,
                "member_id": {"type": "integer", "minimum": 1},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            ("report_id",), read_missing_elder_report,
        ),
    )
    return tuple(RegisteredAgentTool(
        AgentToolDefinition(
            name=name, description=description,
            parameters={
                "type": "object", "properties": properties,
                "required": list(required), "additionalProperties": False,
            },
        ), handler,
    ) for name, description, properties, required, handler in definitions)


async def read_missing_elder_accounts(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.clan_reporting_queries is None:
        return {"error": "Missing Elder evidence is not available."}
    try:
        requested = _clan_codes(arguments.get("clan_codes"))
    except ValueError as error:
        return {"error": str(error)}
    accessible = []
    omitted = []
    for clan_code in requested:
        channel_id = CLAN_LEADERSHIP_CHANNELS[clan_code]
        if await accessible_message_channel(context, channel_id) is None:
            omitted.append(clan_code)
        else:
            accessible.append(clan_code)
    if not accessible:
        return {
            "error": "No selected clan leadership source is accessible.",
            "omitted_inaccessible_clan_codes": omitted,
        }
    try:
        snapshot = context.clan_reporting_queries.missing_elder_snapshot(
            tuple(accessible),
        )
        report = MissingElderReport(
            uuid4().hex, context.guild.id,
            tuple(sorted(
                CLAN_LEADERSHIP_CHANNELS[code] for code in accessible
            )),
            tuple(
                code for code in CLAN_LEADERSHIP_CHANNELS if code in omitted
            ),
            snapshot,
        )
    except (RuntimeError, TypeError, ValueError):
        return {"error": "Missing Elder evidence could not be read completely."}
    context.state.source_channels.update(report.source_channels)
    await require_evidence_access(context)
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {
            "error": (
                "The complete Missing Elder snapshot is too large to retain "
                "in this conversation."
            ),
        }
    try:
        return _page(report, arguments)
    except ValueError as error:
        context.state.reports.pop(report.report_id, None)
        return {"error": str(error)}


async def read_missing_elder_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if (
        not isinstance(report, MissingElderReport)
        or report.guild_id != context.guild.id
    ):
        return {
            "error": (
                "That Missing Elder snapshot is not available in this "
                "conversation."
            ),
        }
    try:
        result = _page(report, arguments)
    except ValueError as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    return result


def _clan_codes(value: Any) -> tuple[str, ...]:
    if value is None:
        return tuple(CLAN_LEADERSHIP_CHANNELS)
    if (
        not isinstance(value, list) or not value
        or len(value) > len(CLAN_LEADERSHIP_CHANNELS)
        or any(not isinstance(code, str) for code in value)
        or len(set(value)) != len(value)
        or any(code not in CLAN_LEADERSHIP_CHANNELS for code in value)
    ):
        raise ValueError("Select one or more known family clan codes.")
    return tuple(value)


def _page(
    report: MissingElderReport, arguments: Mapping[str, Any],
) -> dict[str, Any]:
    return report.page(
        clan_code=arguments.get("clan_code"),
        member_id=arguments.get("member_id"),
        offset=arguments.get("offset", 0),
        limit=arguments.get("limit", 25),
    )


__all__ = [
    "clan_reporting_tools", "read_missing_elder_accounts",
    "read_missing_elder_report",
]
