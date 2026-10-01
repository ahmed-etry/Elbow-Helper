"""Confirmed publication of a lead update to public news."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.configuration.channels import LEAD_NEWS, PUBLIC_NEWS
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass, ChangePreview, PreparedAction
from ..commands.outcomes import CommandOutcome
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_NEWS_PUBLISH_LINE, ACTION_NEWS_PUBLISH_FILE,
    ACTION_NEWS_PUBLISH_CONTENT, ACTION_NEWS_PUBLISH_DONE,
    ACTION_NEWS_PUBLISH_LABEL, ACTION_NEWS_PUBLISH_UNAVAILABLE,
    ACTION_PREVIEW_BLANK,
)
from .discord_safety import check_post_access, check_view_access, resolve_channel


def leadership_news_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(AgentToolDefinition(
        name="publish_lead_news",
        description="Publish an existing lead update to the public news channel after confirmation.",
        parameters={"type": "object", "properties": {
            "message_id": {"type": "integer", "minimum": 1},
        }, "required": ["message_id"], "additionalProperties": False},
    ), prepare_lead_news, AgentCapabilityEffect.COMMAND,
        ActionClass.IRREVERSIBLE, True),)


async def prepare_lead_news(context: AgentRequestContext,
                            values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("LeadNews")
    if workflow is None:
        raise ValueError(ACTION_NEWS_PUBLISH_UNAVAILABLE)
    source = await resolve_channel(context, LEAD_NEWS)
    target = await resolve_channel(context, PUBLIC_NEWS)
    check_view_access(source, context.member, context.guild.me)
    check_post_access(target, context.member, context.guild.me)
    message = await source.fetch_message(values["message_id"])
    prepared = workflow.public_news_preview(message)
    if not prepared["content"] and not prepared["attachments"]:
        raise ValueError(ACTION_NEWS_PUBLISH_UNAVAILABLE)
    lines = [ACTION_NEWS_PUBLISH_LINE.format(
        source=source.mention, target=target.mention)]
    lines.append(ACTION_NEWS_PUBLISH_CONTENT)
    lines.extend(line if line.strip() else ACTION_PREVIEW_BLANK
                 for line in str(prepared["content"]).splitlines())
    lines.extend(ACTION_NEWS_PUBLISH_FILE.format(name=file.filename)
                 for file in prepared["attachments"])
    fingerprint = (prepared["content"], tuple(
        (file.id, file.filename, file.size) for file in prepared["attachments"]))

    async def recheck() -> bool:
        try:
            check_view_access(source, context.member, context.guild.me)
            check_post_access(target, context.member, context.guild.me)
            current = await source.fetch_message(message.id)
        except Exception:
            return False
        latest = workflow.public_news_preview(current)
        return (latest["content"], tuple(
            (file.id, file.filename, file.size) for file in latest["attachments"])) == fingerprint

    async def run() -> CommandOutcome:
        current = await source.fetch_message(message.id)
        sent = await workflow.publish_public_news(
            current, target, prepared=workflow.public_news_preview(current))
        return CommandOutcome("complete", "private",
                              text=ACTION_NEWS_PUBLISH_DONE.format(url=sent.jump_url))

    context.state.command_proposals.append(PreparedAction(
        "publish_lead_news", {"source_channel_id": source.id,
                              "message_id": message.id, "target_channel_id": target.id},
        ChangePreview(tuple(lines), recheck, summary=ACTION_NEWS_PUBLISH_LABEL),
        run, action_class=ActionClass.IRREVERSIBLE,
    ))
    return {"status": "confirmation_required"}
