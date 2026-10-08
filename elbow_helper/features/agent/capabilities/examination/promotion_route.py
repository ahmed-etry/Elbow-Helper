"""Confirmed leadership changes to a promotion route."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord
from elbow_helper.configuration.channels import EXAMINATION_ROOM
from elbow_helper.features.examination.intake.logic import (
    PROMO_SOURCES, is_valid_route, valid_targets_for_source,
)
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...engine.capability_contract import CapabilityContract
from ...access import require_evidence_access
from ...actions.contracts import ActionRefused, ActionClass, ChangePreview, PreparedAction
from ...actions.outcomes import ActionOutcome, embed_text
from ...models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ...wording import (
    ACTION_PROMOTION_ROUTE_LINE,
    ACTION_FIELD_CHANGE,
    ACTION_PROMOTION_ROUTE_REVIEW,
    ACTION_PROMOTION_ROUTE_PROMPT,
    ACTION_PROMOTION_ROUTE_LABEL,
)
from ...discord_actions.safety import check_post_access, check_view_access, resolve_channel


def promotion_route_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(AgentToolDefinition(
        name="read_promotion_review",
        description="Read a promotion review's details or your availability overlap.",
        parameters={"type": "object", "properties": {
            "ticket_channel_id": {"type": "integer", "minimum": 1},
            "view": {"type": "string", "enum": ["details", "availability"]},
        }, "required": ["ticket_channel_id", "view"], "additionalProperties": False},
    ), read_promotion_review,
        contract=CapabilityContract(
            entity_fields=(("ticket_channel_id", "examination_ticket_channel"),),
            source_scope="channel_status",
            channel_fields=("ticket_channel_id",),
            result_channel_fields=("ticket_channel_id", "review_channel_id"),
            filter_fields=("view",),
        ),
            ),
        RegisteredAgentTool(AgentToolDefinition(
        name="change_promotion_route",
        description="Change the current clan and target for a promotion review after confirmation.",
        parameters={"type": "object", "properties": {
            "ticket_channel_id": {"type": "integer", "minimum": 1},
            "from_clan": {"type": "string", "enum": list(PROMO_SOURCES)},
            "to_clan": {"type": "string", "enum": sorted({
                target for source in PROMO_SOURCES
                for target in valid_targets_for_source(source)})},
        }, "required": ["ticket_channel_id", "from_clan", "to_clan"],
            "additionalProperties": False},
    ), prepare_promotion_route, AgentCapabilityEffect.COMMAND,
        ActionClass.CHANGE,
        contract=CapabilityContract(
            entity_fields=(("ticket_channel_id", "examination_ticket_channel"),),
            source_scope="request_context",
            channel_fields=("ticket_channel_id",),
            filter_fields=("from_clan", "to_clan"),
        ),
        ),)


async def read_promotion_review(context: AgentRequestContext,
                                values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("Examination")
    if workflow is None or not workflow.can_change_promotion_route(context.member):
        return {"error": "That promotion request isn't available."}
    case = workflow.promotion_route_snapshot(values["ticket_channel_id"])
    if case is None or not case.get("routing_message_id"):
        return {"error": "That promotion request isn't available."}
    ticket = await resolve_channel(context, values["ticket_channel_id"])
    review = await resolve_channel(context, EXAMINATION_ROOM)
    for channel in (ticket, review):
        check_view_access(channel, context.member, context.guild.me)
    try:
        embed = workflow.build_promotion_review_details(
            case, context.member, context.guild,
            overlap_only=values["view"] == "availability")
    except ValueError as exc:
        return {"error": str(exc)}
    await require_evidence_access(context)
    context.state.source_channels.update((ticket.id, review.id))
    return {"ticket_channel_id": ticket.id, "review_channel_id": review.id,
            "text": embed_text(embed)}


async def prepare_promotion_route(context: AgentRequestContext,
                                  values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("Examination")
    if workflow is None or not workflow.can_change_promotion_route(context.member):
        raise ActionRefused("That promotion request isn't available.")
    case = workflow.promotion_route_snapshot(values["ticket_channel_id"])
    if case is None:
        raise ActionRefused("That promotion request isn't available.")
    ticket = await resolve_channel(context, values["ticket_channel_id"])
    review = await resolve_channel(context, EXAMINATION_ROOM)
    if not isinstance(ticket, discord.TextChannel):
        raise ActionRefused("That promotion request isn't available.")
    for channel in (ticket, review):
        check_post_access(channel, context.member, context.guild.me)
    from_clan, to_clan = values["from_clan"], values["to_clan"]
    if not is_valid_route(from_clan, to_clan):
        return {"status": "needs_input", "issue": "That promotion isn't available from the selected clan.",
                "valid_targets": valid_targets_for_source(from_clan)}
    if case.get("from_clan") == from_clan and case.get("to_clan") == to_clan:
        return {"status": "no_change"}
    lines = [ACTION_PROMOTION_ROUTE_LINE.format(channel=ticket.mention)]
    details = (ACTION_FIELD_CHANGE.format(
        field="Current clan", old=case.get("from_clan") or "Not set", new=from_clan),
        ACTION_FIELD_CHANGE.format(
            field="Promotion target", old=case.get("to_clan") or "Not set", new=to_clan))
    lines.append(ACTION_PROMOTION_ROUTE_REVIEW.format(channel=review.mention))
    if case.get("availability_prompt_id"):
        lines.append(ACTION_PROMOTION_ROUTE_PROMPT.format(
            channel=ticket.mention))

    async def recheck() -> bool:
        try:
            for channel in (ticket, review):
                check_post_access(channel, context.member, context.guild.me)
            return workflow.promotion_route_snapshot(ticket.id) == case
        except ValueError:
            return False

    async def run() -> ActionOutcome:
        try:
            await workflow.change_promotion_route(
                ticket_channel_id=ticket.id,
                routing_message_id=int(case.get("routing_message_id") or 0),
                from_clan=from_clan, to_clan=to_clan,
                actor=context.member,
            )
        except ValueError as error:
            raise ActionRefused(str(error)) from error
        return ActionOutcome("complete", "private", text=ACTION_PROMOTION_ROUTE_LABEL)

    context.state.proposed_changes.append(PreparedAction(
        "change_promotion_route", {"ticket_channel_id": ticket.id},
        ChangePreview(tuple(lines), recheck, summary=ACTION_PROMOTION_ROUTE_LABEL,
                      details=details, detail_sources=frozenset({ticket.id, review.id})),
        run, action_class=ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}
