"""Read the requester's CWL hub placement and channels."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...engine.capability_contract import CapabilityContract
from ...access import require_evidence_access
from ...actions.outcomes import embed_text
from ...models import AgentRequestContext, RegisteredAgentTool


TOOL_CONTRACTS = {
    "read_my_cwl_placement": CapabilityContract(entity_fields=(), source_scope="request_context"),
    "read_my_cwl_channels": CapabilityContract(entity_fields=(), source_scope="request_context"),
}


def cwl_member_hub_tools() -> tuple[RegisteredAgentTool, ...]:
    return tuple(RegisteredAgentTool(AgentToolDefinition(
        name=name, description=description,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    ), handler,
        contract=TOOL_CONTRACTS[name],
    ) for name, description, handler in (
        ("read_my_cwl_placement", "Show where the requester's accounts are placed for the announced CWL rosters.",
         read_my_cwl_placement),
        ("read_my_cwl_channels", "Show the requester's CWL info and war discussion channels.",
         read_my_cwl_channels),
    ))


async def read_my_cwl_placement(context: AgentRequestContext,
                                values: Mapping[str, Any]) -> Mapping[str, Any]:
    return await _read(context, channels_only=False)


async def read_my_cwl_channels(context: AgentRequestContext,
                               values: Mapping[str, Any]) -> Mapping[str, Any]:
    return await _read(context, channels_only=True)


async def _read(context: AgentRequestContext,
                *, channels_only: bool) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("CwlManagement")
    if workflow is None:
        return {"error": "CWL rosters aren't available."}
    issue = await workflow.member_cwl_release_issue(context.guild.id)
    if issue:
        return {"error": issue}
    issue, embed = await workflow.member_cwl_information(
        context.guild.id, context.member.id, channels_only=channels_only)
    await require_evidence_access(context)
    return {"error": issue} if issue else {"text": embed_text(embed)}
