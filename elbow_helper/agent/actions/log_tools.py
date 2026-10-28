"""Read the requester's durable action history."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..engine.capability_contract import CapabilityContract
from ..access import require_evidence_access
from ..disclosure import can_disclose_provenance
from ..models import AgentRequestContext, RegisteredAgentTool
from ..models import AgentCapabilityEffect
from .contracts import ActionClass
from .targets import target_channel_ids, target_links


def action_log_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(
        AgentToolDefinition(
            name="read_agent_action_log",
            description="Read this member's recent confirmed changes and outcomes.",
            parameters={"type": "object", "properties": {
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            }, "required": [], "additionalProperties": False},
        ), read_agent_action_log,
        contract=CapabilityContract(entity_fields=()),
            ), RegisteredAgentTool(
        AgentToolDefinition(
            name="undo_agent_action",
            description="Preview an undo of this member's reversible action.",
            parameters={"type": "object", "properties": {
                "log_id": {"type": "string", "minLength": 1, "maxLength": 32},
            }, "required": ["log_id"], "additionalProperties": False},
        ), undo_agent_action, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
        contract=CapabilityContract(entity_fields=(("log_id", "agent_action_log"),)),
       ))


async def read_agent_action_log(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.action_repository is None:
        return {"error": "Action history is unavailable."}
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", 25)
    rows = await asyncio.to_thread(
        context.action_repository.recent_log,
        requester_id=context.member.id, offset=offset, limit=limit + 1,
    )
    actions = []
    for row in rows[:limit]:
        entry = {key: row[key] for key in ("log_id", "action_class", "outcome", "executed_at")}
        provenance = await _visible_history_sources(context, row)
        if provenance is None:
            entry.update(action_label="Action details are unavailable here.",
                         target_links=(), details_available=False)
        else:
            sources, access, targets = provenance
            context.state.source_channels.update(sources)
            context.state.required_access.update(access)
            entry.update(action_name=row["action_name"], action_label=row["action_label"],
                         target_links=target_links(context.guild.id, targets), details_available=True)
        actions.append(entry)
    await require_evidence_access(context)
    return {
        "actions": actions,
        "next_offset": offset + limit if len(rows) > limit else None,
    }


async def _visible_history_sources(context, row):
    """Unknown legacy provenance cannot authorize old labels or target links."""
    if row.get("guild_id") != context.guild.id:
        return None
    try:
        preview = json.loads(row.get("preview_json") or "null")
        targets = json.loads(row["targets_json"])
        if not isinstance(preview, dict) or not isinstance(targets, dict):
            return None
        sources, access = preview["detail_sources"], preview["detail_access"]
        if (not isinstance(sources, list) or not isinstance(access, list)
                or any(type(value) is not int or value <= 0 for value in sources)
                or any(not isinstance(value, str) for value in access)
                or type(row.get("channel_id")) is not int or row["channel_id"] <= 0):
            return None
        sources = set(sources) | {row["channel_id"]} | set(target_channel_ids(targets))
        access = set(access)
    except (KeyError, TypeError, ValueError):
        return None
    if not await can_disclose_provenance(context, sources, access):
        return None
    return sources, access, targets


async def undo_agent_action(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.action_runner is None:
        return {"error": "Action history is unavailable."}
    try:
        action = await context.action_runner.prepare_undo(context, arguments["log_id"])
    except (RuntimeError, ValueError):
        return {"error": "That change cannot be undone."}
    context.state.proposed_changes.append(action)
    return {"status": "confirmation_required"}
