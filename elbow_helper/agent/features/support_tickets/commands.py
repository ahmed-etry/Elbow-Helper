"""Support ticket commands through their public feature operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord

from elbow_helper.agent.discord_actions.safety import check_member, check_bot_post_access, resolve_channel
from elbow_helper.features.help.discovery import ParameterInfo

from elbow_helper.agent.actions.contracts import ActionRefused, ActionClass, ChangePreview
from elbow_helper.agent.conversation.wording import ACTION_TICKET_SUPPORT_ROLE, ACTION_SUPPORT_OPEN_DEFAULT_TOPIC, ACTION_TICKET_CONTROLS, ACTION_SUPPORT_OPEN_LABEL, ACTION_SUPPORT_OPEN_LINE, ACTION_SUPPORT_OPEN_POST, ACTION_SUPPORT_OPEN_TOPIC, ACTION_SUPPORT_CLOSE_CHANNEL_OPTION, ACTION_SUPPORT_CLOSE_LABEL, ACTION_SUPPORT_CLOSE_LINE, ACTION_SUPPORT_CLOSE_LOG, ACTION_SUPPORT_CLOSE_TRANSCRIPT, ACTION_SUPPORT_CLOSE_HISTORY, ACTION_SUPPORT_CLOSE_CONTROLS, ACTION_SUPPORT_CLOSE_DONE
from elbow_helper.agent.actions.outcomes import ActionOutcome, embed_text
from elbow_helper.agent.commands.registry import CommandAdapter, PreparedCommandChange


def _workflow(context: Any):
    workflow = context.bot.get_cog("SupportActions")
    if workflow is None:
        raise ActionRefused('Support tickets are unavailable.')
    return workflow


async def _member(context: Any, member_id: int):
    member = context.guild.get_member(member_id)
    if member is None:
        try:
            member = await context.guild.fetch_member(member_id)
        except discord.DiscordException:
            raise ActionRefused('That member is unavailable.') from None
    check_member(member, context.guild.me, requester=context.member, guild=context.guild)
    return member


def _target_signature(prepared: Mapping[str, Any]):
    category = prepared["category"]
    return (prepared["user"].id, prepared["name"],
            category.id if category else None,
            tuple(sorted(prepared["visible_roles"])),
            prepared["bot_member_id"], prepared["topic"])


async def prepare_support_open(context: Any,
                               values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = _workflow(context)
    member = await _member(context, values["user"])
    prepared = await workflow.prepare_support_ticket(
        context.guild, member, values["topic"], context.member,
    )
    prepared["via"] = "agent"
    category = prepared["category"]
    lines = [
        ACTION_SUPPORT_OPEN_LINE.format(name=prepared["name"], member=member.mention,
                                       place=f" in {category.mention}" if category else ""),
    ]
    if prepared["visible_roles"]:
        roles = ", ".join(f"<@&{role_id}>" for role_id in sorted(prepared["visible_roles"]))
        lines.append(ACTION_TICKET_SUPPORT_ROLE.format(roles=roles))
    details = (ACTION_SUPPORT_OPEN_TOPIC.format(
        topic=prepared["topic"] or ACTION_SUPPORT_OPEN_DEFAULT_TOPIC),
        ACTION_TICKET_CONTROLS, ACTION_SUPPORT_OPEN_POST,
        member.mention, prepared["welcome"], *embed_text(prepared["embed"]).splitlines())
    signature = _target_signature(prepared)

    async def recheck() -> bool:
        try:
            live_member = await _member(context, member.id)
        except ValueError:
            return False
        live = workflow.support_ticket_target_state(
            context.guild, live_member, values["topic"], context.member,
        )
        return _target_signature(live) == signature

    async def run() -> ActionOutcome:
        channel, _ = await workflow.open_support_ticket(prepared)
        registration = workflow.support_ticket_registration(channel.id)
        if registration is None or registration.get("owner") != member.id:
            raise OSError("Support ticket creation could not be verified")
        return ActionOutcome(
            "complete", "private", text=f"Opened <#{channel.id}> for {member.mention}.",
            result={"channel_id": channel.id}, after={"channel_id": channel.id},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=ACTION_SUPPORT_OPEN_LABEL, details=details,
                      message_body=slice(3, 5)),
        run,
    )


async def run_support_open(context: Any,
                           values: Mapping[str, Any]) -> ActionOutcome:
    return await (await prepare_support_open(context, values)).run()


async def prepare_support_close(context: Any,
                                values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = _workflow(context)
    channel = await resolve_channel(
        context, values.get("channel") or context.source_message.channel.id,
    )
    check_bot_post_access(channel, context.guild.me)
    prepared = workflow.prepare_support_close(
        context.guild, channel, context.member,
    )
    prepared["via"] = "agent"
    if prepared["issue"]:
        raise ActionRefused(prepared["issue"])
    log_channel = prepared["log_channel"]
    if log_channel is None:
        raise ActionRefused("The transcript log channel hasn't been set up.")
    check_bot_post_access(log_channel, context.guild.me)
    owner = prepared["owner"]
    if owner is not None:
        check_member(owner, context.guild.me, requester=context.member, guild=context.guild)
    history = await workflow.support_close_history(channel)
    lines = [
        ACTION_SUPPORT_CLOSE_LINE.format(
            channel=channel.mention,
            access=f" and stop {owner.mention} from sending messages there" if owner else ""),
    ]
    lines.extend((
        ACTION_SUPPORT_CLOSE_LOG.format(channel=log_channel.mention),
        ACTION_SUPPORT_CLOSE_TRANSCRIPT.format(
            limit=context.guild.filesize_limit // 1_048_576,
        ),
    ))
    details = (ACTION_SUPPORT_CLOSE_HISTORY.format(count=len(history)),)
    lines.append(ACTION_SUPPORT_CLOSE_CONTROLS)
    signature = (
        prepared["ticket_info"], prepared["source"],
        owner.id if owner else None,
        prepared["log_channel_id"], prepared["transcript_filename"],
    )

    async def recheck() -> bool:
        try:
            check_bot_post_access(channel, context.guild.me)
            check_bot_post_access(log_channel, context.guild.me)
            live = workflow.prepare_support_close(
                context.guild, channel, context.member,
            )
            if live["issue"]:
                return False
            live_owner = live["owner"]
            if live_owner is not None:
                check_member(live_owner, context.guild.me,
                    requester=context.member, guild=context.guild)
        except ValueError:
            return False
        live_signature = (
            live["ticket_info"], live["source"],
            live_owner.id if live_owner else None,
            live["log_channel_id"], live["transcript_filename"],
        )
        return (live_signature == signature
                and await workflow.support_close_history(channel) == history)

    async def run() -> ActionOutcome:
        reports = []

        async def report(message: str) -> None:
            reports.append(message)

        result = await workflow.close_support_ticket(prepared, report)
        if result["status"] != "complete":
            # The owner was already locked and the close notice posted before the
            # transcript failed, so this is a half-finished close, not "nothing changed".
            raise OSError(result["issue"])
        text = "\n".join(reports) if reports else ACTION_SUPPORT_CLOSE_DONE.format(
            channel=log_channel.mention)
        if not isinstance(result["log_message_id"], int):
            raise OSError("Support ticket close could not be verified")
        return ActionOutcome(
            "complete", "private", text=f"Closed {channel.mention}.\n{text}",
            result={"channel_id": channel.id, "status": result["status"],
                    "log_channel_id": prepared["log_channel_id"],
                    "log_message_id": result.get("log_message_id")},
            after={"channel_id": channel.id, "status": result["status"],
                   "log_message_id": result.get("log_message_id")},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=ACTION_SUPPORT_CLOSE_LABEL,
                      details=details, preview_only_sources=frozenset({channel.id}),
                      before={"channel_id": channel.id,
                              "owner_id": owner.id if owner else None,
                              "history_message_ids": tuple(row[0] for row in history)}),
        run,
    )


async def run_support_close(context: Any,
                            values: Mapping[str, Any]) -> ActionOutcome:
    return await (await prepare_support_close(context, values)).run()


def support_ticket_adapters() -> tuple[CommandAdapter, ...]:
    return (
        CommandAdapter(
            "/open", "confirm", run_support_open,
            prepare=prepare_support_open, action_class=ActionClass.CHANGE,
            capability_name="open_support_ticket",
        ),
        CommandAdapter(
            "/close", "confirm", run_support_close,
            prepare=prepare_support_close, action_class=ActionClass.CHANGE,
            options=(ParameterInfo(
                "channel", ACTION_SUPPORT_CLOSE_CHANNEL_OPTION, False, "channel",
            ),),
            entity_options=(("channel", "discord_channel"),), capability_name="close_ticket",
        ),
    )
