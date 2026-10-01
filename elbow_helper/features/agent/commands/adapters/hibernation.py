"""Hibernation commands through the member-state feature."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord

from elbow_helper.features.agent.tools.discord_safety import (
    check_member, check_role, resolve_member,
)

from ...actions.contracts import ActionClass, ChangePreview
from ...wording import (
    ACTION_HIBERNATE_FALLBACK, ACTION_HIBERNATE_LABEL,
    ACTION_HIBERNATE_LINE, ACTION_HIBERNATE_LOG,
    ACTION_HIBERNATE_MISSING_ROLE, ACTION_HIBERNATE_NOTICE,
    ACTION_HIBERNATE_ROLE_ADD, ACTION_HIBERNATE_ROLE_REMOVE,
    ACTION_HIBERNATE_SAVE_ROLE, ACTION_HIBERNATE_SAVE_RANK,
    ACTION_HIBERNATE_UNAVAILABLE, ACTION_PREVIEW_BLANK,
    ACTION_REACTIVATE_LINE, ACTION_REACTIVATE_ROLE_ADD,
    ACTION_REACTIVATE_ROLE_REMOVE, ACTION_REACTIVATE_ACHIEVEMENT,
    ACTION_REACTIVATE_TICKET, ACTION_REACTIVATE_CATEGORY,
    ACTION_REACTIVATE_NO_CATEGORY, ACTION_REACTIVATE_DEFAULT_DENY,
    ACTION_REACTIVATE_MEMBER_ACCESS, ACTION_REACTIVATE_BOT_ACCESS,
    ACTION_REACTIVATE_ROLE_ACCESS, ACTION_REACTIVATE_CONTROLS,
    ACTION_REACTIVATE_FALLBACK, ACTION_REACTIVATE_LABEL,
)
from ..outcomes import CommandOutcome, embed_text
from ..registry import CommandAdapter, PreparedCommandChange


async def prepare_hibernate(context: Any,
                            values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = context.bot.get_cog("Hibernate")
    if workflow is None:
        raise ValueError(ACTION_HIBERNATE_UNAVAILABLE)
    member = await resolve_member(context.guild, values["user"])
    check_member(member, context.guild.me)
    plan = workflow.prepare_hibernation(context.guild, member)
    if plan["issue"]:
        raise ValueError(plan["issue"])
    if plan["missing_role_ids"]:
        raise ValueError(ACTION_HIBERNATE_MISSING_ROLE.format(
            roles=", ".join(f"<@&{role_id}>" for role_id in plan["missing_role_ids"]),
        ))

    def check_roles(current_plan) -> None:
        for role in (*current_plan["to_remove"], *current_plan["to_add"]):
            check_role(role, context.guild, context.guild.me, {})

    check_roles(plan)
    notice = workflow.hibernation_notice_preview(member)
    lines = [ACTION_HIBERNATE_LINE.format(member=member.mention,
                                          date=f"<t:{plan['unix_ts']}:F>")]
    lines.extend(ACTION_HIBERNATE_SAVE_ROLE.format(role=f"<@&{role_id}>")
                 for role_id in plan["stored_role_ids"])
    lines.extend(ACTION_HIBERNATE_SAVE_RANK.format(role=f"<@&{role_id}>")
                 for role_id in plan["snapshot_role_ids"])
    lines.extend(ACTION_HIBERNATE_ROLE_REMOVE.format(
        role=role.mention, member=member.mention,
    ) for role in plan["to_remove"])
    lines.extend(ACTION_HIBERNATE_ROLE_ADD.format(
        role=role.mention, member=member.mention,
    ) for role in plan["to_add"])
    lines.append(ACTION_HIBERNATE_LOG.format(channel=f"<#{plan['log_channel_id']}>"))
    lines.append(ACTION_HIBERNATE_NOTICE.format(member=member.mention))
    lines.append(ACTION_HIBERNATE_FALLBACK.format(
        channel=f"<#{plan['fallback_channel_id']}>",
    ))
    lines.extend(line or ACTION_PREVIEW_BLANK for line in notice.splitlines())
    signature = (
        tuple(plan["stored_role_ids"]), tuple(plan["snapshot_role_ids"]),
        tuple(role.id for role in plan["to_remove"]),
        tuple(role.id for role in plan["to_add"]), notice,
    )

    async def recheck() -> bool:
        try:
            current_member = await resolve_member(context.guild, member.id, fresh=True)
            check_member(current_member, context.guild.me)
            current = workflow.prepare_hibernation(context.guild, current_member)
            if current["issue"] or current["missing_role_ids"]:
                return False
            check_roles(current)
        except (ValueError, discord.DiscordException):
            return False
        return signature == (
            tuple(current["stored_role_ids"]), tuple(current["snapshot_role_ids"]),
            tuple(role.id for role in current["to_remove"]),
            tuple(role.id for role in current["to_add"]),
            workflow.hibernation_notice_preview(current_member),
        )

    async def run() -> CommandOutcome:
        message = await workflow.hibernate_member(context.guild, context.member, plan)
        state = workflow.hibernation_member_state(member.id)
        if state is None or state.get("roles") != list(plan["stored_role_ids"]):
            raise OSError("Hibernation state could not be verified")
        return CommandOutcome(
            "complete", "private", text=message,
            result={"member_id": member.id},
            after={"member_id": member.id, "state": state},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=ACTION_HIBERNATE_LABEL,
                      before={"member_id": member.id,
                              "role_ids": tuple(role.id for role in member.roles)}),
        run,
    )


async def run_hibernate(context: Any,
                        values: Mapping[str, Any]) -> CommandOutcome:
    return await (await prepare_hibernate(context, values)).run()


async def prepare_reactivate(context: Any,
                             values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = context.bot.get_cog("Hibernate")
    if workflow is None:
        raise ValueError(ACTION_HIBERNATE_UNAVAILABLE)
    target_id = values.get("user") or context.member.id
    target = await resolve_member(context.guild, target_id)
    check_member(target, context.guild.me)
    force = target.id != context.member.id
    plan = workflow.prepare_reactivation(
        guild=context.guild, actor=context.member,
        target=target, force_reactivate=force,
    )
    if plan["issue"]:
        raise ValueError(plan["issue"])
    if plan["missing_role_ids"]:
        raise ValueError(ACTION_HIBERNATE_MISSING_ROLE.format(
            roles=", ".join(f"<@&{role_id}>" for role_id in plan["missing_role_ids"]),
        ))

    def check_roles(current_plan) -> None:
        for role in (*current_plan["to_remove"], *current_plan["to_add"]):
            check_role(role, context.guild, context.guild.me, {})

    check_roles(plan)
    ticket = plan["ticket"]
    category = ticket["category"]
    lines = [ACTION_REACTIVATE_LINE.format(member=target.mention)]
    lines.extend(ACTION_REACTIVATE_ROLE_REMOVE.format(
        role=role.mention, member=target.mention,
    ) for role in plan["to_remove"])
    lines.extend(ACTION_REACTIVATE_ROLE_ADD.format(
        role=role.mention, member=target.mention,
    ) for role in plan["to_add"])
    lines.append(ACTION_REACTIVATE_ACHIEVEMENT.format(member=target.mention))
    lines.append(ACTION_REACTIVATE_TICKET.format(name=ticket["name"]))
    lines.append(ACTION_REACTIVATE_CATEGORY.format(
        category=category.mention if category else ACTION_REACTIVATE_NO_CATEGORY,
    ))
    lines.extend((
        ACTION_REACTIVATE_DEFAULT_DENY,
        ACTION_REACTIVATE_MEMBER_ACCESS.format(member=target.mention),
    ))
    if ticket["bot_member_id"]:
        lines.append(ACTION_REACTIVATE_BOT_ACCESS)
    lines.extend(ACTION_REACTIVATE_ROLE_ACCESS.format(role=f"<@&{role_id}>")
                 for role_id in ticket["visible_role_ids"])
    lines.extend(line or ACTION_PREVIEW_BLANK
                 for line in ticket["welcome"].splitlines())
    lines.extend(line or ACTION_PREVIEW_BLANK
                 for line in embed_text(ticket["embed"]).splitlines())
    lines.append(ACTION_REACTIVATE_CONTROLS)
    if plan["fallback_thread_id"]:
        lines.append(ACTION_REACTIVATE_FALLBACK.format(
            thread=f"<#{plan['fallback_thread_id']}>",
        ))

    def signature(item):
        ticket = item["ticket"]
        return (
            item["info"], tuple(role.id for role in item["to_remove"]),
            tuple(role.id for role in item["to_add"]),
            item["fallback_thread_id"], ticket["name"],
            ticket["category"].id if ticket["category"] else None,
            ticket["visible_role_ids"], ticket["bot_member_id"],
            ticket["welcome"], ticket["embed"].to_dict(),
        )

    initial = signature(plan)

    async def recheck() -> bool:
        try:
            live_target = await resolve_member(context.guild, target.id, fresh=True)
            check_member(live_target, context.guild.me)
            current = workflow.prepare_reactivation(
                guild=context.guild, actor=context.member,
                target=live_target, force_reactivate=force,
            )
            if current["issue"] or current["missing_role_ids"]:
                return False
            check_roles(current)
        except (ValueError, discord.DiscordException):
            return False
        return signature(current) == initial

    async def run() -> CommandOutcome:
        result = await workflow.reactivate_member(plan)
        if workflow.hibernation_member_state(target.id) is not None:
            raise OSError("Reactivation state could not be verified")
        channel = result["ticket"]
        if channel.id != result["ticket_channel_id"]:
            raise OSError("Reactivation ticket could not be verified")
        return CommandOutcome(
            "complete", "private", text=result["message"],
            result={"member_id": target.id,
                    "ticket_channel_id": result["ticket_channel_id"]},
            after={"member_id": target.id,
                   "ticket_channel_id": result["ticket_channel_id"]},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=ACTION_REACTIVATE_LABEL,
                      before={"member_id": target.id, "state": plan["info"],
                              "role_ids": tuple(role.id for role in target.roles)}),
        run,
    )


async def run_reactivate(context: Any,
                         values: Mapping[str, Any]) -> CommandOutcome:
    return await (await prepare_reactivate(context, values)).run()


def hibernation_adapters() -> tuple[CommandAdapter, ...]:
    return (
        CommandAdapter(
            "/hibernate", "confirm", run_hibernate,
            prepare=prepare_hibernate, action_class=ActionClass.IRREVERSIBLE,
            entity_options=(("user", "discord_member"),),
        ),
        CommandAdapter(
            "/reactivate", "confirm", run_reactivate,
            prepare=prepare_reactivate, action_class=ActionClass.IRREVERSIBLE,
            entity_options=(("user", "discord_member"),),
        ),
    )
