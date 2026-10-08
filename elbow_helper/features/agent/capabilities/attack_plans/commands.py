"""Attack plan posts through the planning feature."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord

from elbow_helper.features.agent.discord_actions.safety import check_post_access

from ...actions.contracts import ActionRefused, ActionClass, ChangePreview
from ...wording import (
    ACTION_PLAN_BASE,
    ACTION_PLAN_LABEL,
    ACTION_PLAN_LINE,
    ACTION_PLAN_PAGE,
    ACTION_PLAN_PINGS,
    ACTION_PLAN_STRATEGY,
    ACTION_PREVIEW_BLANK,
)
from ...actions.outcomes import ActionOutcome, embed_text
from ...commands.registry import CommandAdapter, PreparedCommandChange


def _image(context: Any, attachment_id: int):
    return next((attachment for attachment in context.source_message.attachments
                 if attachment.id == attachment_id), None)


async def prepare_attack_plan(context: Any,
                              values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = context.bot.get_cog("Planning")
    if workflow is None:
        raise ActionRefused('Attack plans are unavailable.')
    channel = context.source_message.channel
    check_post_access(channel, context.member, context.guild.me)
    strategy_image = _image(context, values["strategy_image"])
    base_image = _image(context, values["base_image"])
    if strategy_image is None or base_image is None:
        raise ActionRefused('Attach both screenshots to your message.')
    player_tag, issue = await workflow.resolve_plan_account(values["player"])
    if issue:
        raise ActionRefused("More than one Clash account matches that name. Use a player tag." if issue == "ambiguous"
                         else "That Clash account is invalid. Use a player tag or an exact account name.")
    prepared = await workflow.prepare_attack_plan(
        player_tag, values["thinking"], strategy_image, base_image,
    )
    if prepared["issue"]:
        raise ActionRefused(prepared["issue"])
    lines = [ACTION_PLAN_LINE.format(channel=channel.mention)]
    if prepared["mention_roles"]:
        lines.append(ACTION_PLAN_PINGS.format(roles=prepared["mention_roles"]))
    lines.extend((
        ACTION_PLAN_STRATEGY.format(url=strategy_image.url),
        ACTION_PLAN_BASE.format(url=base_image.url),
    ))
    details = []
    for index, page in enumerate(prepared["embeds"].pages, start=1):
        details.append(ACTION_PLAN_PAGE.format(number=index))
        for embed in prepared["embeds"].embeds_for_page(index - 1):
            details.extend(line or ACTION_PREVIEW_BLANK
                         for line in embed_text(embed).splitlines())

    async def recheck() -> bool:
        try:
            check_post_access(channel, context.member, context.guild.me)
        except ValueError:
            return False
        return (_image(context, values["strategy_image"]) is strategy_image
                and _image(context, values["base_image"]) is base_image
                and workflow.validate_plan_inputs(
                    player_tag, strategy_image, base_image,
                )[1] is None)

    async def run() -> ActionOutcome:
        async def send(**kwargs):
            return await channel.send(
                **kwargs,
                allowed_mentions=discord.AllowedMentions(
                    users=False, roles=True, everyone=False,
                ),
            )

        message = await workflow.post_attack_plan(prepared, send)
        return ActionOutcome("complete", result={
            "channel_id": channel.id, "message_id": message.id,
        }, after={"channel_id": channel.id, "message_id": message.id})

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=ACTION_PLAN_LABEL,
                      details=tuple(details), detail_sources=frozenset({channel.id})), run,
    )


async def run_attack_plan(context: Any,
                          values: Mapping[str, Any]) -> ActionOutcome:
    return await (await prepare_attack_plan(context, values)).run()


def attack_plan_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter("/plan", "confirm", run_attack_plan,
                           prepare=prepare_attack_plan,
                           action_class=ActionClass.CHANGE, capability_name="attack_plan_help"),)
