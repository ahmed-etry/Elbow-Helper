"""Read the requester's durable action history."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..models import AgentRequestContext, RegisteredAgentTool
from ..models import AgentCapabilityEffect
from ..actions.contracts import ActionClass


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
    ), RegisteredAgentTool(
        AgentToolDefinition(
            name="undo_agent_action",
            description="Preview an undo of one completed reversible action from this member's log.",
            parameters={"type": "object", "properties": {
                "log_id": {"type": "string", "minLength": 1, "maxLength": 32},
            }, "required": ["log_id"], "additionalProperties": False},
        ), undo_agent_action, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE, True,
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
    await require_evidence_access(context)
    return {
        "actions": [{
            "log_id": row["log_id"],
            "action_name": row["action_name"],
            "action_label": row["action_label"],
            "action_class": row["action_class"],
            "targets": json.loads(row["targets_json"]),
            "outcome": row["outcome"],
            "executed_at": row["executed_at"],
        } for row in rows[:limit]],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


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
    context.state.command_proposals.append(action)
    return {"status": "confirmation_required"}
