"""Read-only achievement economy and raffle adapters."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...engine.capability_contract import CapabilityContract
from ...access import ACCESS_LEAD, require_lookup_access, require_evidence_access
from ...models import AgentRequestContext, RegisteredAgentTool
TOOL_CONTRACTS = {
    "read_achievement_economy_rules": CapabilityContract(entity_fields=()),
    "read_member_inventory": CapabilityContract(
        entity_fields=(("member_id", "discord_member"),),
    ),
}


def achievement_economy_tools() -> tuple[RegisteredAgentTool, ...]:
    definitions = (
        (
            "read_achievement_economy_rules",
            "Read the configured coin, ticket, salary, manual-award cap and achievement-reward "
            "rules from the owning feature. This returns current rules, not a member balance, "
            "raffle result or permission to grant rewards.",
            {},
            (),
            read_achievement_economy_rules,
        ),
        (
            "read_member_inventory",
            "Read one current member's coin balance and current-month raffle-ticket status "
            "using the existing inventory rules. A requester may read their own inventory; "
            "viewing another member requires current Lead access. This performs no economy "
            "or raffle action.",
            {"member_id": {"type": "integer", "minimum": 1}},
            ("member_id",),
            read_member_inventory,
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
        contract=TOOL_CONTRACTS[name],
    ) for name, description, properties, required, handler in definitions)


async def read_achievement_economy_rules(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    del arguments
    await require_evidence_access(context)
    if context.achievement_queries is None:
        return {"error": "Achievement economy rules are not available."}
    snapshot = await asyncio.to_thread(context.achievement_queries.economy_rules)
    await require_evidence_access(context)
    return {
        "observed_at": snapshot.observed_at,
        "daily_activity": {
            "qualifying_messages": snapshot.daily_message_threshold,
            "minimum_characters_per_message": snapshot.daily_minimum_characters,
            "member_coins": snapshot.daily_member_reward,
            "elder_coins": snapshot.daily_elder_reward,
        },
        "elder_monthly_salary_coins": snapshot.elder_monthly_salary,
        "tickets": {
            "cost_coins": snapshot.ticket_cost,
            "limit_per_month": snapshot.ticket_limit_per_month,
        },
        "manual_monthly_caps": {
            "cwl_coins": snapshot.manual_cwl_monthly_cap,
            "encouragement_coins": snapshot.manual_encouragement_monthly_cap,
            "combined_coins": snapshot.manual_combined_monthly_cap,
        },
        "achievement_reward_total_coins": sum(
            value for _, value in snapshot.achievement_rewards
        ),
        "achievement_rewards": dict(snapshot.achievement_rewards),
    }


async def read_member_inventory(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    member_id = arguments["member_id"]
    requires_lead = member_id != context.member.id
    if requires_lead:
        require_lookup_access(context, {ACCESS_LEAD})
    if context.achievement_queries is None:
        return {"error": "Achievement economy data is not available."}
    if context.guild.get_member(member_id) is None:
        return {"error": "That member is not currently in this server."}
    try:
        snapshot = await asyncio.to_thread(
            context.achievement_queries.member_inventory, member_id,
        )
    except (RuntimeError, ValueError):
        return {"error": "Member inventory could not be read completely."}
    await require_evidence_access(context)
    if requires_lead:
        require_lookup_access(context, {ACCESS_LEAD})
    current_member = context.guild.get_member(member_id)
    if current_member is None:
        return {"error": "That member is not currently in this server."}
    if requires_lead:
        context.state.required_access.add(ACCESS_LEAD)
    return {
        "observed_at": snapshot.observed_at, "member_id": snapshot.member_id,
        "member_name": current_member.display_name, "balance": snapshot.balance,
        "has_current_ticket": snapshot.has_current_ticket,
        "current_month_key": snapshot.current_month_key,
    }
__all__ = ["achievement_economy_tools", "read_achievement_economy_rules", "read_member_inventory"]
