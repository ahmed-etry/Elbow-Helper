"""Read and resolve recruiter account suggestions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.configuration.channels import CLAN_LEADERSHIP_CHANNELS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.features.account_links.config import REVIEW_CHANNEL_ID
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...engine.capability_contract import CapabilityContract
from ...access import require_evidence_access
from ...actions.contracts import ActionClass, ChangePreview, PreparedAction
from ...actions.outcomes import ActionOutcome
from ...models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ...wording import (
    ACTION_ACCOUNT_ADD_LINE,
    ACTION_SUGGESTION_OLD,
    ACTION_SUGGESTION_IGNORE,
    ACTION_SUGGESTION_REVIEW,
    ACTION_SUGGESTION_BOARD,
    ACTION_SUGGESTION_LINK_LABEL,
    ACTION_SUGGESTION_IGNORE_LABEL,
)
from ...discord_actions.safety import (
    check_member, check_post_access, check_view_access,
    resolve_channel, resolve_member,
)


def account_suggestion_tools() -> tuple[RegisteredAgentTool, ...]:
    tag_schema = {"type": "object", "properties": {
        "player_tag": {"type": "string"},
        "member_id": {"type": "integer", "minimum": 1},
    }, "required": ["player_tag"], "additionalProperties": False}
    return (
        RegisteredAgentTool(AgentToolDefinition(
            name="read_account_suggestions",
            description="Read pending recruiter account matches in the review channel.",
            parameters={"type": "object", "properties": {
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            }, "additionalProperties": False},
        ), read_account_suggestions,
            contract=CapabilityContract(
                entity_fields=(),
                time_fields=(),
                source_scope="channel_status",
                result_channel_fields=("review_channel_id",),
                filter_fields=("offset", "limit"),
            ),
        ),
        RegisteredAgentTool(AgentToolDefinition(
            name="link_account_suggestion",
            description="Confirm or correct a pending account match after confirmation.",
            parameters=tag_schema,
        ), prepare_suggestion_link, AgentCapabilityEffect.COMMAND,
            ActionClass.CHANGE,
            contract=CapabilityContract(
                entity_fields=(("player_tag", "clash_account"), ("member_id", "discord_member")),
                time_fields=(),
                source_scope="request_context",
            ),
        ),
        RegisteredAgentTool(AgentToolDefinition(
            name="ignore_account_suggestion",
            description="Permanently ignore a pending account match after confirmation.",
            parameters={"type": "object", "properties": {
                "player_tag": {"type": "string"},
            }, "required": ["player_tag"], "additionalProperties": False},
        ), prepare_suggestion_ignore, AgentCapabilityEffect.COMMAND,
            ActionClass.IRREVERSIBLE,
            contract=CapabilityContract(
                entity_fields=(("player_tag", "clash_account"),),
                time_fields=(),
                source_scope="request_context",
            ),
        ),
    )


async def _workflow(context: AgentRequestContext):
    await require_evidence_access(context)
    workflow = context.bot.get_cog("AccountLinks")
    if workflow is None or not workflow.can_review_links(context.member):
        raise ValueError("That account suggestion isn't available.")
    review = await resolve_channel(context, REVIEW_CHANNEL_ID)
    check_view_access(review, context.member, context.guild.me)
    return workflow, review


async def read_account_suggestions(context: AgentRequestContext,
                                   values: Mapping[str, Any]) -> Mapping[str, Any]:
    workflow, review = await _workflow(context)
    rows = workflow.list_pending_suggestions()
    offset, limit = values.get("offset", 0), values.get("limit", 25)
    await require_evidence_access(context)
    return {"review_channel_id": review.id, "total": len(rows), "offset": offset,
            "suggestions": [{"player_tag": row.get("player_tag"),
                             "player_name": row.get("player_name"),
                             "clan_code": row.get("current_clan_code"),
                             "suggested_member_id": row.get("proposed_discord_user_id")}
                            for row in rows[offset:offset + limit]]}


async def prepare_suggestion_link(context: AgentRequestContext,
                                  values: Mapping[str, Any]) -> Mapping[str, Any]:
    return await _prepare(context, values, ignore=False)


async def prepare_suggestion_ignore(context: AgentRequestContext,
                                    values: Mapping[str, Any]) -> Mapping[str, Any]:
    return await _prepare(context, values, ignore=True)


async def _prepare(context: AgentRequestContext, values: Mapping[str, Any],
                   *, ignore: bool) -> Mapping[str, Any]:
    workflow, review = await _workflow(context)
    check_post_access(review, context.member, context.guild.me)
    tag = normalize_player_tag(values["player_tag"])
    if tag is None:
        return {"status": "needs_input", "issue": "That player tag is not valid.",
                "prepared_count": 0}
    suggestion = workflow.account_suggestion_snapshot(tag)
    if suggestion is None:
        raise ValueError("That account suggestion isn't available.")
    member = None
    old = workflow.get_links_by_tags([tag]).get(tag)
    if not ignore:
        member_id = values.get("member_id") or int(
            suggestion.get("proposed_discord_user_id") or 0)
        if not member_id:
            return {"status": "needs_input", "issue": "Which member owns this Clash account?",
                    "prepared_count": 0}
        member = await resolve_member(context.guild, member_id)
        check_member(member, context.guild.me)
    board_id = CLAN_LEADERSHIP_CHANNELS.get(str(suggestion.get("current_clan_code") or ""))
    board = await resolve_channel(context, board_id) if board_id and not ignore else None
    if board is not None:
        check_post_access(board, context.member, context.guild.me)
    lines = [(ACTION_SUGGESTION_IGNORE if ignore else ACTION_ACCOUNT_ADD_LINE).format(
        name=suggestion.get("player_name") or tag, tag=tag,
        member=member.mention if member else "")]
    if old:
        lines.append(ACTION_SUGGESTION_OLD.format(member=f"<@{old['discord_user_id']}>"))
    if suggestion.get("review_message_id"):
        lines.append(ACTION_SUGGESTION_REVIEW.format(
            channel=review.mention))
    if board is not None:
        lines.append(ACTION_SUGGESTION_BOARD.format(channel=board.mention))

    async def recheck() -> bool:
        try:
            check_post_access(review, context.member, context.guild.me)
            if board is not None:
                check_post_access(board, context.member, context.guild.me)
            if member is not None:
                check_member(member, context.guild.me)
            return (workflow.account_suggestion_snapshot(tag) == suggestion
                    and workflow.get_links_by_tags([tag]).get(tag) == old)
        except ValueError:
            return False

    async def run() -> ActionOutcome:
        proposed = int(suggestion.get("proposed_discord_user_id") or 0)
        message = await workflow.resolve_account_suggestion(
            tag, context.member,
            discord_user_id=(member.id if member and member.id != proposed else None),
            ignore=ignore)
        return ActionOutcome("complete", "private", text=message)

    label = ACTION_SUGGESTION_IGNORE_LABEL if ignore else ACTION_SUGGESTION_LINK_LABEL
    context.state.proposed_changes.append(PreparedAction(
        "ignore_account_suggestion" if ignore else "link_account_suggestion",
        {"player_tag": tag, **({"member_id": member.id} if member else {})},
        ChangePreview(tuple(lines), recheck, summary=label),
        run, action_class=(ActionClass.IRREVERSIBLE if ignore else ActionClass.CHANGE),
    ))
    return {"status": "confirmation_required"}
