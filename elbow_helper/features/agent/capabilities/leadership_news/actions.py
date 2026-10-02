"""Confirmed publication of a lead update to public news."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import discord

from elbow_helper.configuration.channels import LEAD_NEWS, PUBLIC_NEWS
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...engine.capability_contract import CapabilityContract
from ...access import require_evidence_access
from ...actions.contracts import ActionClass, ChangePreview, PreparedAction
from ...actions.outcomes import ActionOutcome
from ...models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ...wording import (
    ACTION_NEWS_PUBLISH_LINE,
    ACTION_NEWS_PUBLISH_FILE,
    ACTION_NEWS_PUBLISH_CONTENT,
    ACTION_NEWS_PUBLISH_DONE,
    ACTION_NEWS_PUBLISH_LABEL,
    ACTION_PREVIEW_BLANK,
    ACTION_NEWS_DISMISS_LINE,
    ACTION_NEWS_DISMISS_LABEL,
    ACTION_NEWS_PUBLISH_PROMPT,
)
from ...discord_actions.safety import check_post_access, check_view_access, resolve_channel


def leadership_news_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(AgentToolDefinition(
        name="publish_lead_news",
        description="Publish an existing lead update to the public news channel after confirmation.",
        parameters={"type": "object", "properties": {
            "message_id": {"type": "integer", "minimum": 1},
        }, "required": ["message_id"], "additionalProperties": False},
    ), prepare_lead_news, AgentCapabilityEffect.COMMAND,
        ActionClass.CHANGE,
        contract=CapabilityContract(
            entity_fields=(("message_id", "discord_message"),),
            time_fields=(),
            source_scope="request_context",
        ),
            ),
        RegisteredAgentTool(AgentToolDefinition(
            name="dismiss_lead_news_prompt",
            description="Dismiss a lead update's publication prompt without publishing it.",
            parameters={"type": "object", "properties": {
                "prompt_message_id": {"type": "integer", "minimum": 1},
            }, "required": ["prompt_message_id"], "additionalProperties": False},
        ), prepare_news_dismiss, AgentCapabilityEffect.COMMAND,
            ActionClass.IRREVERSIBLE,
            contract=CapabilityContract(
                entity_fields=(("prompt_message_id", "discord_message"),),
                time_fields=(),
                source_scope="request_context",
            ),
        ),)


async def prepare_news_dismiss(context: AgentRequestContext,
                               values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("LeadNews")
    if workflow is None:
        raise ValueError('That lead update is unavailable.')
    source = await resolve_channel(context, LEAD_NEWS)
    check_post_access(source, context.member, context.guild.me)
    prompt = await source.fetch_message(values["prompt_message_id"])
    source_id = workflow.public_news_prompt_source(prompt)
    if source_id is None:
        raise ValueError('That lead update is unavailable.')
    lines = (ACTION_NEWS_DISMISS_LINE.format(
        channel=source.mention),)

    async def recheck() -> bool:
        try:
            check_post_access(source, context.member, context.guild.me)
            current = await source.fetch_message(prompt.id)
            return workflow.public_news_prompt_source(current) == source_id
        except (discord.DiscordException, ValueError, RuntimeError, KeyError, TypeError, OSError):
            return False

    async def run() -> ActionOutcome:
        current = await source.fetch_message(prompt.id)
        if not await workflow.dismiss_public_news_prompt(current):
            raise ValueError('That lead update is unavailable.')
        return ActionOutcome("complete", "private", text=ACTION_NEWS_DISMISS_LABEL)

    context.state.proposed_changes.append(PreparedAction(
        "dismiss_lead_news_prompt", {"prompt_message_id": prompt.id},
        ChangePreview(lines, recheck, summary=ACTION_NEWS_DISMISS_LABEL),
        run, action_class=ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}


async def prepare_lead_news(context: AgentRequestContext,
                            values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("LeadNews")
    if workflow is None:
        raise ValueError('That lead update is unavailable.')
    source = await resolve_channel(context, LEAD_NEWS)
    target = await resolve_channel(context, PUBLIC_NEWS)
    check_view_access(source, context.member, context.guild.me)
    check_post_access(target, context.member, context.guild.me)
    message = await source.fetch_message(values["message_id"])
    prepared = workflow.public_news_preview(message)
    if not prepared["content"] and not prepared["attachments"]:
        raise ValueError('That lead update is unavailable.')
    lines = [ACTION_NEWS_PUBLISH_LINE.format(
        target=target.mention)]
    lines.append(ACTION_NEWS_PUBLISH_CONTENT)
    lines.extend(line if line.strip() else ACTION_PREVIEW_BLANK
                 for line in str(prepared["content"]).splitlines())
    lines.extend(ACTION_NEWS_PUBLISH_FILE.format(name=file.filename)
                 for file in prepared["attachments"])
    prompts = await workflow.find_public_news_prompts(message)
    lines.extend(ACTION_NEWS_PUBLISH_PROMPT.format(
        channel=source.mention) for prompt in prompts)
    fingerprint = (prepared["content"], tuple(
        (file.id, file.filename, file.size) for file in prepared["attachments"]))

    async def recheck() -> bool:
        try:
            check_view_access(source, context.member, context.guild.me)
            check_post_access(target, context.member, context.guild.me)
            current = await source.fetch_message(message.id)
        except (discord.DiscordException, ValueError, RuntimeError, KeyError, TypeError, OSError):
            return False
        latest = workflow.public_news_preview(current)
        latest_prompts = await workflow.find_public_news_prompts(current)
        return ((latest["content"], tuple(
            (file.id, file.filename, file.size) for file in latest["attachments"])) == fingerprint
                and tuple(prompt.id for prompt in latest_prompts)
                == tuple(prompt.id for prompt in prompts))

    async def run() -> ActionOutcome:
        current = await source.fetch_message(message.id)
        current_prompts = await workflow.find_public_news_prompts(current)
        sent = await workflow.publish_public_news(
            current, target, prepared=workflow.public_news_preview(current),
            prompts=current_prompts)
        return ActionOutcome("complete", "private",
                              text=ACTION_NEWS_PUBLISH_DONE.format(url=sent.jump_url))

    context.state.proposed_changes.append(PreparedAction(
        "publish_lead_news", {"source_channel_id": source.id,
                              "message_id": message.id, "target_channel_id": target.id},
        ChangePreview(tuple(lines), recheck, summary=ACTION_NEWS_PUBLISH_LABEL),
        run, action_class=ActionClass.IRREVERSIBLE,
    ))
    return {"status": "confirmation_required"}
