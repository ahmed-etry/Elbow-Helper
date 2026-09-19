"""Permission-aware reads of feature-owned role-connection rules."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from elbow_helper.features.role_connections.queries import connection_matches
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import (
    ACCESS_LEAD, require_access_requirements, require_evidence_access,
)
from ..models import AgentRequestContext, RegisteredAgentTool


def role_connection_tools() -> tuple[RegisteredAgentTool, ...]:
    return (
        RegisteredAgentTool(
            AgentToolDefinition(
                name="read_role_connections",
                description=(
                    "Read the configured rules that automatically add or remove "
                    "Discord roles. Optionally evaluate each returned rule against one "
                    "current member's roles. Requires current Lead access; malformed "
                    "and cyclic rules remain explicit, and this never runs a scan or "
                    "changes roles."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "member_id": {"type": "integer", "minimum": 1},
                        "offset": {"type": "integer", "minimum": 0},
                        "limit": {
                            "type": "integer", "minimum": 1, "maximum": 25,
                        },
                        "expected_state_fingerprint": {
                            "type": "string",
                            "pattern": "^[0-9a-f]{64}$",
                        },
                    },
                    "additionalProperties": False,
                },
            ),
            read_role_connections,
        ),
    )


async def read_role_connections(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    require_access_requirements(
        context.guild, context.member.id, {ACCESS_LEAD},
    )
    context.state.required_access.add(ACCESS_LEAD)
    if context.role_connection_queries is None:
        return {"error": "Role-connection evidence is not available."}

    member = None
    member_id = arguments.get("member_id")
    if member_id is not None:
        member = context.guild.get_member(member_id)
        if member is None:
            return {"error": "That member is not currently in this server."}
    try:
        snapshot = context.role_connection_queries.snapshot()
    except RuntimeError:
        return {"error": "Role-connection evidence could not be read completely."}

    await require_evidence_access(context)
    if member_id is not None:
        member = context.guild.get_member(member_id)
        if member is None:
            return {"error": "That member is not currently in this server."}
    expected = arguments.get("expected_state_fingerprint")
    if expected is not None and expected != snapshot.state_fingerprint:
        return {
            "error": (
                "Role-connection rules changed. Read the first page again before "
                "continuing."
            ),
            "state_fingerprint": snapshot.state_fingerprint,
        }

    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", 25)
    page = snapshot.rules[offset:offset + limit]
    member_role_ids = (
        frozenset(role.id for role in member.roles) if member is not None else None
    )
    return {
        "observed_at": snapshot.observed_at,
        "state_fingerprint": snapshot.state_fingerprint,
        "total_entries": snapshot.total_entries,
        "valid_rules": len(snapshot.rules),
        "malformed_rule_count": len(snapshot.malformed_indexes),
        "malformed_rule_indexes": list(snapshot.malformed_indexes),
        "cyclic_rule_count": sum(rule.cyclic for rule in snapshot.rules),
        "evaluated_member": (
            {
                "member_id": member.id,
                "member_name": member.display_name,
                "role_ids": sorted(member_role_ids),
            }
            if member is not None else None
        ),
        "rules": [
            _render_rule(context, rule, member_role_ids)
            for rule in page
        ],
        "next_offset": offset + limit if offset + limit < len(snapshot.rules) else None,
        "complete_valid_rule_snapshot": True,
        "all_entries_valid": not snapshot.malformed_indexes,
    }


def _render_rule(context: AgentRequestContext, rule, member_role_ids):
    def role_name(role_id: int) -> str | None:
        role = context.guild.get_role(role_id)
        return role.name if role is not None else None

    value = {
        "index": rule.index,
        "connection_id": rule.connection_id,
        "target_role_id": rule.target_role_id,
        "target_role_name": role_name(rule.target_role_id),
        "all_conditions": [
            {**asdict(condition), "role_name": role_name(condition.role_id)}
            for condition in rule.all_conditions
        ],
        "any_conditions": [
            {**asdict(condition), "role_name": role_name(condition.role_id)}
            for condition in rule.any_conditions
        ],
        "cyclic": rule.cyclic,
    }
    if member_role_ids is not None:
        value.update({
            "member_currently_has_target": rule.target_role_id in member_role_ids,
            "rule_matches_member": (
                None if rule.cyclic else connection_matches(member_role_ids, rule)
            ),
        })
    return value


__all__ = ["read_role_connections", "role_connection_tools"]
