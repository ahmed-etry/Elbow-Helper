"""Check access to a lookup before its handler runs."""

from collections.abc import Mapping
from typing import Any

from elbow_helper.agent.actions.contracts import ActionClass
from elbow_helper.agent.engine.models import AgentRequestContext
from elbow_helper.agent.engine.tool_call import channel_scope_refusal
from elbow_helper.agent.reports.tools import original_tool
from elbow_helper.agent.safety.access import (
    accessible_message_channel, has_lookup_access, message_channel_unavailable,
    require_evidence_access,
)


async def disclosure_issue(
    context: AgentRequestContext, registry: Mapping[str, Any], step: Mapping[str, Any]
) -> dict[str, Any]:
    selected = original_tool(registry, step["capability"], step["arguments"])
    if (selected or registry[step["capability"]]).action_class in (
        ActionClass.CHANGE, ActionClass.IRREVERSIBLE,
    ):
        return {}
    contract = (
        selected.contract if selected else registry[step["capability"]].contract
    )
    if contract is None:
        return {}
    channels: set[int] = set()
    for field in contract.channel_fields:
        value = step["arguments"].get(field)
        if type(value) is int:
            channels.add(value)
        elif isinstance(value, list):
            channels.update(item for item in value if type(item) is int)
    await require_evidence_access(context)
    if not has_lookup_access(context, contract.required_access):
        return channel_scope_refusal(context, {
            "error": "This needs " + ", ".join(sorted(contract.required_access)) + " access.",
            "required_access": sorted(contract.required_access), "cause": "access_denied"})
    for channel_id in channels:
        if await accessible_message_channel(context, channel_id) is None:
            result = message_channel_unavailable(context, channel_id)
            result.setdefault("cause", "access_denied" if str(channel_id) in context.state.scope_audit.excluded
                              else "unavailable")
            result.setdefault("available_privately", False)
            return result

    return {}
