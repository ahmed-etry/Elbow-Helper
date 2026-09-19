"""Read-only achievement economy and raffle adapters."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping
from uuid import uuid4

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import ACCESS_LEAD, require_access_requirements, require_evidence_access
from ..reports.achievement import CoinTransactionReport, RaffleReport
from ..reports.base import ArtifactCapacityError, retain_report
from ..models import AgentRequestContext, RegisteredAgentTool


def achievement_economy_tools() -> tuple[RegisteredAgentTool, ...]:
    report_id = {"type": "string", "minLength": 1, "maxLength": 32}
    offset = {"type": "integer", "minimum": 0}
    limit = {"type": "integer", "minimum": 1, "maximum": 25}
    definitions = (
        (
            "read_achievement_economy_rules",
            "Read the configured coin, ticket, salary, manual-award cap and achievement-reward rules from the owning feature. This returns current rules, not a member balance, raffle result or permission to grant rewards.",
            {}, (), read_achievement_economy_rules,
        ),
        (
            "read_member_inventory",
            "Read one current member's coin balance and current-month raffle-ticket status using the existing inventory rules. A requester may read their own inventory; viewing another member requires current Lead access. This performs no economy or raffle action.",
            {"member_id": {"type": "integer", "minimum": 1}},
            ("member_id",), read_member_inventory,
        ),
        (
            "read_member_coin_history",
            "Read and retain one current member's newest coin earnings and spending. Returns the exact total and explicitly reports if the retained history is bounded rather than complete. This performs no economy or raffle action.",
            {"member_id": {"type": "integer", "minimum": 1}, "limit": limit},
            ("member_id",), read_member_coin_history,
        ),
        (
            "read_member_coin_history_report",
            "Read another page from one retained coin-transaction snapshot without rereading mutable economy data.",
            {"report_id": report_id, "offset": offset, "limit": limit},
            ("report_id",), read_member_coin_history_report,
        ),
        (
            "read_raffle",
            "Read and retain the stored raffle prize, ticket holders, configured winner count and draw history for the current month or an exact YYYY-MM month. This matches the existing Core/Lead Plus read boundary and never buys, grants, removes, draws, rerolls or clears anything.",
            {
                "month": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}$"},
                "limit": limit,
            },
            (), read_raffle,
        ),
        (
            "read_raffle_report",
            "Read more ticket holders or winner history from one retained raffle-month snapshot without rereading mutable raffle data.",
            {
                "report_id": report_id, "ticket_offset": offset,
                "winner_offset": offset, "limit": limit,
            },
            ("report_id",), read_raffle_report,
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
        require_access_requirements(context.guild, context.member.id, {ACCESS_LEAD})
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
        require_access_requirements(context.guild, context.member.id, {ACCESS_LEAD})
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


async def read_member_coin_history(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.achievement_queries is None:
        return {"error": "Achievement economy data is not available."}
    member_id = arguments["member_id"]
    if context.guild.get_member(member_id) is None:
        return {"error": "That member is not currently in this server."}
    try:
        snapshot = await asyncio.to_thread(
            context.achievement_queries.coin_transactions, member_id,
        )
    except (RuntimeError, ValueError):
        return {"error": "Coin history could not be read completely."}
    await require_evidence_access(context)
    current_member = context.guild.get_member(member_id)
    if current_member is None:
        return {"error": "That member is not currently in this server."}
    try:
        report = CoinTransactionReport(
            uuid4().hex, context.guild.id, current_member.display_name, snapshot,
        )
    except ValueError:
        return {"error": "Coin history could not be read completely."}
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {"error": "The complete retained coin-history snapshot is too large for this conversation."}
    return report.page(limit=arguments.get("limit", 25))


async def read_member_coin_history_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if not isinstance(report, CoinTransactionReport) or report.guild_id != context.guild.id:
        return {"error": "That coin-history report is not available in this conversation."}
    try:
        result = report.page(
            offset=arguments.get("offset", 0), limit=arguments.get("limit", 25),
        )
    except ValueError as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    return result


async def read_raffle(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.achievement_queries is None:
        return {"error": "Raffle data is not available."}
    try:
        snapshot = await asyncio.to_thread(
            context.achievement_queries.raffle, arguments.get("month"),
        )
    except ValueError:
        return {"error": "Use a valid raffle month in YYYY-MM format."}
    except RuntimeError:
        return {"error": "Raffle data could not be read completely."}
    await require_evidence_access(context)
    member_ids = set(snapshot.ticket_member_ids) | {
        row.member_id for row in snapshot.winners
    }
    try:
        report = RaffleReport(
            uuid4().hex, context.guild.id, snapshot,
            tuple(sorted(
                (member_id, member.display_name)
                for member_id in member_ids
                if (member := context.guild.get_member(member_id)) is not None
            )),
        )
    except ValueError:
        return {"error": "Raffle data could not be read completely."}
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {"error": "The retained raffle snapshot is too large for this conversation."}
    return report.page(limit=arguments.get("limit", 25))


async def read_raffle_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if not isinstance(report, RaffleReport) or report.guild_id != context.guild.id:
        return {"error": "That raffle report is not available in this conversation."}
    try:
        result = report.page(
            ticket_offset=arguments.get("ticket_offset", 0),
            winner_offset=arguments.get("winner_offset", 0),
            limit=arguments.get("limit", 25),
        )
    except ValueError as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    return result


__all__ = [
    "achievement_economy_tools", "read_achievement_economy_rules",
    "read_member_coin_history",
    "read_member_coin_history_report", "read_member_inventory",
    "read_raffle", "read_raffle_report",
]
