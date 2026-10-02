"""Rosters post commands."""

from __future__ import annotations
from collections.abc import Mapping
from typing import Any
import discord
from elbow_helper.features.agent.discord_actions.safety import check_post_access, resolve_channel
from elbow_helper.features.help.discovery import ParameterInfo
from ...actions.contracts import ActionClass, ChangePreview
from ...models import AgentAttachment
from ...wording import (
    ACTION_ROSTER_POST_REFRESH,
    ACTION_ROSTER_POST_ACCOUNTS,
    ACTION_ROSTER_POST_CHANNEL_OPTION,
    ACTION_ROSTER_POST_CLOSED,
    ACTION_ROSTER_POST_CONTROLS,
    ACTION_ROSTER_POST_HIDDEN,
    ACTION_ROSTER_POST_LABEL,
    ACTION_ROSTER_POST_LINE,
    ACTION_ROSTER_POST_OPEN,
    ACTION_ROSTER_POST_RESET,
    ACTION_ROSTER_POST_PAGE,
    ACTION_ROSTER_POST_IMAGE,
    ACTION_PREVIEW_BLANK,
    ACTION_ROSTER_EXPORT_ACCOUNT,
    ACTION_ROSTER_EXPORT_FILE,
    ACTION_ROSTER_EXPORT_GOOGLE,
    ACTION_ROSTER_EXPORT_LABEL,
    ACTION_ROSTER_EXPORT_LINE,
    ACTION_ROSTER_EXPORT_LINK,
)
from ...actions.outcomes import CommandOutcome, embed_text
from ...commands.registry import CommandAdapter, PreparedCommandChange


async def prepare_roster_post(context: Any,
                              values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError('That roster is unavailable.')
    try:
        roster_id = int(values["roster"])
    except (TypeError, ValueError):
        raise ValueError('That roster is unavailable.') from None
    roster = await workflow.get_roster(roster_id)
    if roster is None or roster.guild_id != context.guild.id:
        raise ValueError('That roster is unavailable.')
    channel = await resolve_channel(
        context, values.get("channel") or context.source_message.channel.id,
    )
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        raise ValueError('That roster is unavailable.')
    check_post_access(channel, context.member, context.guild.me)
    state = await workflow.roster_edit_state(roster)
    render = await workflow.preview_roster_post(roster)
    effect = render["effect"]

    shown_count = 0 if effect["clears_signups"] else state["account_count"]
    lines = [
        ACTION_ROSTER_POST_LINE.format(name=roster.name, channel=channel.mention),
        ACTION_ROSTER_POST_OPEN if effect["opens"] else ACTION_ROSTER_POST_CLOSED,
        ACTION_ROSTER_POST_ACCOUNTS.format(count=shown_count),
        ACTION_ROSTER_POST_HIDDEN if roster.buttons_hidden else ACTION_ROSTER_POST_CONTROLS,
    ]
    if effect["clears_signups"]:
        lines.append(ACTION_ROSTER_POST_RESET)
    lines.extend(ACTION_ROSTER_POST_REFRESH.format(
        channel=f"<#{channel_id}>", message_id=message_id,
    ) for channel_id, message_id in state["posts"])
    for page_number, embeds in enumerate(render["pages"], start=1):
        lines.append(ACTION_ROSTER_POST_PAGE.format(number=page_number))
        for embed in embeds:
            lines.extend(line or ACTION_PREVIEW_BLANK
                         for line in embed_text(embed).splitlines())
            for visual in (embed.image, embed.thumbnail):
                if visual.url:
                    lines.append(ACTION_ROSTER_POST_IMAGE.format(url=visual.url))

    async def recheck() -> bool:
        current = await workflow.get_roster(roster_id)
        if current != roster or workflow.roster_post_effect(roster) != effect:
            return False
        try:
            check_post_access(channel, context.member, context.guild.me)
        except ValueError:
            return False
        if await workflow.roster_edit_state(roster) != state:
            return False
        live_render = await workflow.preview_roster_post(roster)
        return live_render["signature"] == render["signature"]

    async def run() -> CommandOutcome:
        if workflow.roster_post_effect(roster) != effect:
            raise ValueError("Roster post changed before execution")
        result = await workflow.post_roster(
            roster_id, channel.send, rendered=render["rendered"],
        )
        if result is None:
            raise ValueError('That roster is unavailable.')
        opened, message = result
        if not await workflow.roster_post_registered(roster_id, message.id):
            raise OSError("Roster post could not be verified")
        return CommandOutcome(
            "complete", "public",
            result={"roster_id": roster_id, "channel_id": channel.id,
                    "message_id": message.id},
            after={"roster_id": roster_id, "status": opened.status,
                   "message_id": message.id},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_POST_LABEL,
                      before={"roster_id": roster.id, "status": roster.status}),
        run,
    )


async def run_roster_post(context: Any,
                          values: Mapping[str, Any]) -> CommandOutcome:
    return await (await prepare_roster_post(context, values)).run()


async def prepare_roster_export(context: Any,
                                values: Mapping[str, Any]) -> PreparedCommandChange | CommandOutcome:
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError('That roster is unavailable.')
    try:
        roster_id = int(values["roster"])
    except (TypeError, ValueError):
        raise ValueError('That roster is unavailable.') from None
    roster = await workflow.get_roster(roster_id)
    if roster is None or roster.guild_id != context.guild.id:
        raise ValueError('That roster is unavailable.')
    plan = await workflow.roster_export_plan(roster)
    if not plan["accounts"]:
        return CommandOutcome(
            "complete", "private", text=f"No accounts are signed up to **{roster.name}**.",
        )
    lines = [
        ACTION_ROSTER_EXPORT_LINE.format(name=roster.name, roster_id=roster.id),
        ACTION_ROSTER_EXPORT_FILE.format(name=plan["workbook_name"]),
        ACTION_ROSTER_EXPORT_GOOGLE,
    ]
    lines.extend(ACTION_ROSTER_EXPORT_ACCOUNT.format(
        tag=tag, member=f"<@{member_id}>",
    ) for tag, member_id in plan["accounts"])

    async def recheck() -> bool:
        current = await workflow.get_roster(roster_id)
        if current != roster:
            return False
        fresh = await workflow.roster_export_plan(roster)
        return fresh["accounts"] == plan["accounts"]

    async def run() -> CommandOutcome:
        report, warning = await workflow.export_roster(
            roster, timestamp=plan["timestamp"],
        )
        if report is None:
            return CommandOutcome("complete", "private", text=warning or "")
        link, data = await workflow.deliver_roster_export(report)
        text = f"Exported **{roster.name}**."
        if link:
            text += "\n" + ACTION_ROSTER_EXPORT_LINK.format(url=link)
        elif report.google_warning:
            text += "\n" + report.google_warning
        attachments = ((AgentAttachment(report.workbook_name, data),)
                       if data is not None else ())
        return CommandOutcome(
            "complete", "private", text=text,
            attachments=attachments,
            result={"roster_id": roster_id, "workbook_name": report.workbook_name,
                    "google_link": link},
            after={"roster_id": roster_id, "workbook_name": report.workbook_name,
                   "google_link": link},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_EXPORT_LABEL,
                      before={"roster_id": roster.id,
                              "accounts": plan["accounts"]}),
        run,
    )


async def run_roster_export(context: Any,
                            values: Mapping[str, Any]) -> CommandOutcome:
    prepared = await prepare_roster_export(context, values)
    return await prepared.run() if isinstance(prepared, PreparedCommandChange) else prepared


def post_adapters() -> tuple[CommandAdapter, ...]:
    return (
        CommandAdapter("/roster post", "confirm", run_roster_post,
                       prepare=prepare_roster_post,
                       action_class=ActionClass.CHANGE,
                       options=(ParameterInfo(
                           "channel", ACTION_ROSTER_POST_CHANNEL_OPTION,
                           False, "channel",
                       ),),
                       entity_options=(("roster", "roster"),
                                       ("channel", "discord_channel"))),
        CommandAdapter("/roster export", "private", run_roster_export,
                       entity_options=(("roster", "roster"),)),
    )
