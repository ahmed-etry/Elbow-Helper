"""Confirmed thread creation, state changes, and membership."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass, ChangePreview, PreparedAction, audit_reason
from ..commands.outcomes import CommandOutcome
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_THREAD_ACTIONS, ACTION_THREAD_MEMBER_ADD, ACTION_THREAD_MEMBER_REMOVE,
    ACTION_THREAD_CREATE_LABEL, ACTION_THREAD_CREATE_LINE,
    ACTION_THREAD_MEMBER_LABEL, ACTION_THREAD_MEMBER_LINE,
    ACTION_THREAD_FUTURE_TARGET,
    ACTION_THREAD_UPDATE_LABEL, ACTION_THREAD_UPDATE_LINE,
    ACTION_UNDO_CHANGED,
)
from .discord_safety import (DiscordActionRefused, check_member, check_view_access,
                             resolve_channel, resolve_member)


THREAD_OPERATIONS = ("rename", "archive", "unarchive", "lock", "unlock")
MEMBER_OPERATIONS = ("add", "remove")


def discord_thread_tools() -> tuple[RegisteredAgentTool, ...]:
    return (
        RegisteredAgentTool(AgentToolDefinition(
            name="create_discord_thread",
            description="Create a public or private thread in a channel. Forum threads need initial_message; for text channels, plan a later post for the opening message. Returns thread_id for later actions.",
            parameters={"type": "object", "properties": {
                "parent_channel_id": {"type": "integer", "minimum": 1},
                "name": {"type": "string", "minLength": 1, "maxLength": 100},
                "private": {"type": "boolean"},
                "initial_message": {"type": "string", "minLength": 1, "maxLength": 2000},
            }, "required": ["parent_channel_id", "name"], "additionalProperties": False},
        ), prepare_create_thread, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE, True),
        RegisteredAgentTool(AgentToolDefinition(
            name="update_discord_thread",
            description="Rename, archive, unarchive, lock or unlock a thread after confirmation.",
            parameters={"type": "object", "properties": {
                "thread_id": {"type": "integer", "minimum": 1},
                "operation": {"type": "string", "enum": list(THREAD_OPERATIONS)},
                "name": {"type": "string", "minLength": 1, "maxLength": 100},
            }, "required": ["thread_id", "operation"], "additionalProperties": False},
        ), prepare_update_thread, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE, True),
        RegisteredAgentTool(AgentToolDefinition(
            name="change_discord_thread_members",
            description="Add or remove selected members of a thread after confirmation.",
            parameters={"type": "object", "properties": {
                "thread_id": {"type": "integer", "minimum": 1},
                "operation": {"type": "string", "enum": list(MEMBER_OPERATIONS)},
                "member_ids": {"type": "array", "items": {"type": "integer", "minimum": 1},
                               "minItems": 1, "maxItems": 25, "uniqueItems": True},
            }, "required": ["thread_id", "operation", "member_ids"],
               "additionalProperties": False},
        ), prepare_thread_members, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE, True),
    )


async def _thread(context: AgentRequestContext, thread_id: int,
                  *, fresh: bool = False) -> Any:
    thread = (await context.bot.fetch_channel(thread_id) if fresh else
              await resolve_channel(context, thread_id))
    if not isinstance(thread, discord.Thread) or thread.guild.id != context.guild.id:
        raise DiscordActionRefused("That thread is unavailable.")
    check_view_access(thread, context.member, context.guild.me)
    return thread


async def _parent(context: AgentRequestContext, channel_id: int,
                  *, private: bool) -> Any:
    channel = await resolve_channel(context, channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        raise DiscordActionRefused("Choose a text or forum channel for the thread.")
    check_view_access(channel, context.member, context.guild.me)
    if private and isinstance(channel, discord.ForumChannel):
        raise DiscordActionRefused("Forum threads cannot be private.")
    permission = "create_private_threads" if private else "create_public_threads"
    for actor in (context.member, context.guild.me):
        if not getattr(channel.permissions_for(actor), permission, False):
            raise DiscordActionRefused("Both you and the bot need permission to create that thread.")
    return channel


async def prepare_create_thread(context: AgentRequestContext,
                                arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    try:
        parent = await _parent(context, arguments["parent_channel_id"],
                               private=bool(arguments.get("private", False)))
    except DiscordActionRefused as error:
        return {"error": str(error), "prepared_count": 0}
    if isinstance(parent, discord.ForumChannel) and not arguments.get("initial_message"):
        return {"error": "A forum thread needs its opening message.", "prepared_count": 0}
    if isinstance(parent, discord.TextChannel) and arguments.get("initial_message"):
        return {"error": "Post the opening message after creating the text thread.",
                "prepared_count": 0}
    name = arguments["name"].strip()
    private = bool(arguments.get("private", False))
    initial = arguments.get("initial_message")
    created_id: int | None = None

    async def recheck() -> bool:
        try:
            await _parent(context, parent.id, private=private)
        except DiscordActionRefused:
            return False
        return True

    async def run() -> CommandOutcome:
        nonlocal created_id
        live_parent = await _parent(context, parent.id, private=private)
        if isinstance(live_parent, discord.ForumChannel):
            created = await live_parent.create_thread(
                name=name, content=initial,
                allowed_mentions=discord.AllowedMentions.none(),
                reason=audit_reason(context.member),
            )
            thread = created.thread
        else:
            thread = await live_parent.create_thread(
                name=name, type=(discord.ChannelType.private_thread if private else
                                 discord.ChannelType.public_thread),
                reason=audit_reason(context.member),
            )
        created_id = thread.id
        return CommandOutcome(
            "complete",
            after={"thread_id": thread.id},
            result={"thread_id": thread.id, "channel_id": thread.id},
        )

    async def verify() -> bool:
        if created_id is None:
            return False
        try:
            thread = await _thread(context, created_id, fresh=True)
        except (DiscordActionRefused, discord.NotFound):
            return False
        return thread.name == name

    lines = (ACTION_THREAD_CREATE_LINE.format(name=name, channel=parent.mention),)
    if initial:
        lines += tuple(initial.splitlines())
    context.state.command_proposals.append(PreparedAction(
        "create_discord_thread", dict(arguments),
        ChangePreview(lines, recheck, summary=ACTION_THREAD_CREATE_LABEL),
        run, verify=verify, permission="Manage Threads",
    ))
    return {"status": "confirmation_required"}


def _thread_change(operation: str, name: str | None = None) -> tuple[str, Any, str]:
    if operation == "rename":
        if not name or not name.strip():
            raise DiscordActionRefused("Choose a name for the thread.")
        return "name", name.strip(), f" to {name.strip()}"
    return {
        "archive": ("archived", True, ""),
        "unarchive": ("archived", False, ""),
        "lock": ("locked", True, ""),
        "unlock": ("locked", False, ""),
    }[operation]


def _thread_update_action(context: AgentRequestContext, thread_id: int,
                          *, field: str, before: Any, after: Any,
                          operation: str, detail: str = "", undo: bool = False,
                          changed: bool = False) -> PreparedAction:
    verb = ACTION_THREAD_ACTIONS[operation][0]
    async def recheck() -> bool:
        try:
            thread = await _thread(context, thread_id, fresh=True)
        except (DiscordActionRefused, discord.NotFound):
            return False
        return getattr(thread, field) == before

    async def run() -> CommandOutcome:
        thread = await _thread(context, thread_id, fresh=True)
        await thread.edit(**{field: after}, reason=audit_reason(context.member))
        return CommandOutcome(
            "complete", after={field: after}, result={"thread_id": thread_id},
        )

    async def verify() -> bool:
        thread = await _thread(context, thread_id, fresh=True)
        return getattr(thread, field) == after

    thread = context.guild.get_channel_or_thread(thread_id)
    label = thread.mention if thread is not None else f"thread {thread_id}"
    lines = (ACTION_THREAD_UPDATE_LINE.format(
        action=verb, thread=label, detail=detail,
    ), *((ACTION_UNDO_CHANGED,) if changed else ()))
    return PreparedAction(
        "undo_discord_thread_update" if undo else "update_discord_thread",
        {"thread_id": thread_id, "operation": operation,
         **({"name": after} if field == "name" else {})},
        ChangePreview(lines, recheck, summary=ACTION_THREAD_UPDATE_LABEL,
                      before={field: before}),
        run, verify=verify, permission="Manage Threads",
    )


async def prepare_update_thread(context: AgentRequestContext,
                                arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    reference = arguments["thread_id"]
    if isinstance(reference, Mapping) and set(reference) == {"step", "path"}:
        try:
            field, after, detail = _thread_change(
                arguments["operation"], arguments.get("name"),
            )
        except DiscordActionRefused as error:
            return {"error": str(error), "prepared_count": 0}
        verb = ACTION_THREAD_ACTIONS[arguments["operation"]][0]
        label = ACTION_THREAD_FUTURE_TARGET.format(step=reference["step"])
        async def bind(results: Mapping[str, Mapping[str, Any]]) -> PreparedAction:
            from ..plan.executor import resolve_arguments
            thread_id = resolve_arguments({"thread_id": reference}, results)["thread_id"]
            if type(thread_id) is not int:
                raise DiscordActionRefused("The earlier action did not return a thread.")
            thread = await _thread(context, thread_id)
            return _thread_update_action(
                context, thread_id, field=field, before=getattr(thread, field),
                after=after, operation=arguments["operation"], detail=detail,
            )
        async def recheck() -> bool:
            return True
        async def unavailable() -> CommandOutcome:
            raise RuntimeError("The earlier action result was not bound")
        context.state.command_proposals.append(PreparedAction(
            "update_discord_thread", dict(arguments),
            ChangePreview((ACTION_THREAD_UPDATE_LINE.format(
                action=verb, thread=label, detail=detail,
            ),), recheck, summary=ACTION_THREAD_UPDATE_LABEL),
            unavailable, permission="Manage Threads", bind=bind,
        ))
        return {"status": "confirmation_required"}
    try:
        thread = await _thread(context, reference)
        field, after, detail = _thread_change(
            arguments["operation"], arguments.get("name"),
        )
    except DiscordActionRefused as error:
        return {"error": str(error), "prepared_count": 0}
    before = getattr(thread, field)
    if before == after:
        return {"status": "no_change", "prepared_count": 0}
    context.state.command_proposals.append(_thread_update_action(
        context, thread.id, field=field, before=before, after=after,
        operation=arguments["operation"], detail=detail,
    ))
    return {"status": "confirmation_required"}


async def prepare_thread_update_undo(context: AgentRequestContext,
                                     log: Mapping[str, Any]) -> PreparedAction:
    values = log["targets"]
    thread = await _thread(context, values["thread_id"])
    field = next(iter(log["before"]))
    expected = log["after"][field]
    previous = log["before"][field]
    operation = ("rename" if field == "name" else
                 ("archive" if previous else "unarchive") if field == "archived" else
                 ("lock" if previous else "unlock"))
    detail = f" to {previous}" if field == "name" else ""
    return _thread_update_action(
        context, thread.id, field=field, before=expected, after=previous,
        operation=operation, detail=detail, undo=True,
        changed=getattr(thread, field) != expected,
    )


async def _member_ids(thread: Any) -> frozenset[int]:
    return frozenset(item.id for item in await thread.fetch_members())


def _thread_member_action(context: AgentRequestContext, thread_id: int,
                          member_id: int, *, add: bool, before: bool,
                          undo: bool = False, changed: bool = False) -> PreparedAction:
    verb, _, relation = (ACTION_THREAD_MEMBER_ADD if add
                            else ACTION_THREAD_MEMBER_REMOVE)
    async def recheck() -> bool:
        try:
            thread = await _thread(context, thread_id, fresh=True)
            member = await resolve_member(context.guild, member_id, fresh=True)
            check_member(member, context.guild.me)
        except (DiscordActionRefused, discord.NotFound):
            return False
        return (member_id in await _member_ids(thread)) == before

    async def run() -> CommandOutcome:
        thread = await _thread(context, thread_id, fresh=True)
        member = await resolve_member(context.guild, member_id, fresh=True)
        check_member(member, context.guild.me)
        if before != add:
            if add:
                await thread.add_user(member)
            else:
                await thread.remove_user(member)
        return CommandOutcome("complete",
                              after={"has_member": add},
                              result={"thread_id": thread_id})

    async def verify() -> bool:
        thread = await _thread(context, thread_id, fresh=True)
        return (member_id in await _member_ids(thread)) == add

    thread = context.guild.get_channel_or_thread(thread_id)
    member = context.guild.get_member(member_id)
    label_thread = thread.mention if thread is not None else f"thread {thread_id}"
    label_member = member.mention if member is not None else f"member {member_id}"
    lines = (ACTION_THREAD_MEMBER_LINE.format(
        action=verb, member=label_member,
        relation=relation, thread=label_thread,
    ), *((ACTION_UNDO_CHANGED,) if changed else ()))
    return PreparedAction(
        "undo_discord_thread_member" if undo else "change_discord_thread_members",
        {"thread_id": thread_id, "member_id": member_id,
         "operation": "add" if add else "remove"},
        ChangePreview(lines, recheck, summary=ACTION_THREAD_MEMBER_LABEL,
                      before={"has_member": before}),
        run, verify=verify, permission="Manage Threads",
    )


async def prepare_thread_members(context: AgentRequestContext,
                                 arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    reference = arguments["thread_id"]
    if isinstance(reference, Mapping) and set(reference) == {"step", "path"}:
        add = arguments["operation"] == "add"
        label = ACTION_THREAD_FUTURE_TARGET.format(step=reference["step"])
        selected = []
        try:
            for member_id in arguments["member_ids"]:
                member = await resolve_member(context.guild, member_id)
                check_member(member, context.guild.me)
                selected.append(member)
        except DiscordActionRefused as error:
            return {"error": str(error), "prepared_count": 0}
        for member in selected:
            async def bind(results: Mapping[str, Mapping[str, Any]],
                           member_id: int = member.id) -> PreparedAction:
                from ..plan.executor import resolve_arguments
                thread_id = resolve_arguments({"thread_id": reference}, results)["thread_id"]
                if type(thread_id) is not int:
                    raise DiscordActionRefused("The earlier action did not return a thread.")
                thread = await _thread(context, thread_id)
                before = member_id in await _member_ids(thread)
                return _thread_member_action(
                    context, thread_id, member_id, add=add, before=before,
                )
            async def recheck() -> bool:
                return True
            async def unavailable() -> CommandOutcome:
                raise RuntimeError("The earlier action result was not bound")
            verb, _, relation = (ACTION_THREAD_MEMBER_ADD if add
                                 else ACTION_THREAD_MEMBER_REMOVE)
            context.state.command_proposals.append(PreparedAction(
                "change_discord_thread_members",
                {"thread_id": dict(reference), "member_id": member.id,
                 "operation": arguments["operation"]},
                ChangePreview((ACTION_THREAD_MEMBER_LINE.format(
                    action=verb, member=member.mention, relation=relation,
                    thread=label,
                ),), recheck, summary=ACTION_THREAD_MEMBER_LABEL),
                unavailable, permission="Manage Threads", bind=bind,
            ))
        return {"status": "confirmation_required", "prepared_count": len(selected)}
    try:
        thread = await _thread(context, reference)
        present = await _member_ids(thread)
        selected = []
        for member_id in arguments["member_ids"]:
            member = await resolve_member(context.guild, member_id)
            check_member(member, context.guild.me)
            add = arguments["operation"] == "add"
            if (member_id in present) != add:
                selected.append(member_id)
    except DiscordActionRefused as error:
        return {"error": str(error), "prepared_count": 0}
    for member_id in selected:
        context.state.command_proposals.append(_thread_member_action(
            context, thread.id, member_id, add=add, before=not add,
        ))
    return {"status": "confirmation_required" if selected else "no_change",
            "prepared_count": len(selected)}


async def prepare_thread_member_undo(context: AgentRequestContext,
                                     log: Mapping[str, Any]) -> PreparedAction:
    values = log["targets"]
    thread = await _thread(context, values["thread_id"])
    present = values["member_id"] in await _member_ids(thread)
    expected = log["after"]["has_member"]
    return _thread_member_action(
        context, thread.id, values["member_id"],
        add=log["before"]["has_member"], before=expected,
        undo=True, changed=present != expected,
    )
