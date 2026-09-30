"""Confirmed reactions and pins with fresh message preconditions."""

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
    ACTION_PIN_ADD, ACTION_PIN_LABEL, ACTION_PIN_LINE,
    ACTION_PIN_REMOVE, ACTION_REACTION_ADD_LINE,
    ACTION_REACTION_LABEL, ACTION_REACTION_REMOVE_LINE,
    ACTION_UNDO_CHANGED,
)
from .discord_safety import DiscordActionRefused, check_view_access, resolve_channel


def discord_message_control_tools() -> tuple[RegisteredAgentTool, ...]:
    identity = {
        "channel_id": {"type": "integer", "minimum": 1},
        "message_id": {"type": "integer", "minimum": 1},
    }
    return (
        RegisteredAgentTool(AgentToolDefinition(
            name="change_bot_reaction",
            description="Add or remove the bot's reaction on a visible message. Remove never touches another member's reaction.",
            parameters={"type": "object", "properties": {
                **identity,
                "operation": {"type": "string", "enum": ["add", "remove"]},
                "emoji": {"type": "string", "minLength": 1, "maxLength": 100},
            }, "required": ["channel_id", "message_id", "operation", "emoji"],
               "additionalProperties": False},
        ), prepare_reaction, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE, True),
        RegisteredAgentTool(AgentToolDefinition(
            name="change_discord_pin",
            description="Pin or unpin a visible message after confirmation.",
            parameters={"type": "object", "properties": {
                **identity,
                "operation": {"type": "string", "enum": ["pin", "unpin"]},
            }, "required": ["channel_id", "message_id", "operation"],
               "additionalProperties": False},
        ), prepare_pin, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE, True),
    )


async def _message(context: AgentRequestContext, channel_id: int,
                   message_id: int) -> tuple[Any, Any]:
    channel = await resolve_channel(context, channel_id)
    check_view_access(channel, context.member, context.guild.me)
    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound as error:
        raise DiscordActionRefused("That message is unavailable.") from error
    return channel, message


def _reaction_active(message: Any, emoji: str) -> bool:
    return any(str(item.emoji) == emoji and item.me
               for item in message.reactions)


def _control_action(context: AgentRequestContext, *, channel_id: int,
                    message_id: int, kind: str, add: bool, before: bool,
                    emoji: str = "", undo: bool = False,
                    changed: bool = False, label_message: str = "") -> PreparedAction:
    reaction = kind == "reaction"
    verb = (ACTION_PIN_ADD if add else ACTION_PIN_REMOVE)[0] if not reaction else ""

    async def current() -> tuple[Any, Any, bool]:
        channel, message = await _message(context, channel_id, message_id)
        active = (_reaction_active(message, emoji) if reaction else message.pinned)
        return channel, message, active

    async def recheck() -> bool:
        try:
            _, _, active = await current()
        except DiscordActionRefused:
            return False
        return active == before

    async def run() -> CommandOutcome:
        _, message, active = await current()
        if active != add:
            if reaction:
                if add:
                    await message.add_reaction(emoji)
                else:
                    await message.remove_reaction(emoji, context.guild.me)
            elif add:
                await message.pin(reason=audit_reason(context.member))
            else:
                await message.unpin(reason=audit_reason(context.member))
        return CommandOutcome("complete", after={"active": add},
                              result={"message_id": message_id, "channel_id": channel_id})

    async def verify() -> bool:
        _, _, active = await current()
        return active == add

    label = label_message
    line = ((ACTION_REACTION_ADD_LINE if add else ACTION_REACTION_REMOVE_LINE).format(
        emoji=emoji, message=label,
    )
            if reaction else ACTION_PIN_LINE.format(action=verb, message=label))
    return PreparedAction(
        ("undo_bot_reaction" if reaction else "undo_discord_pin") if undo else
        ("change_bot_reaction" if reaction else "change_discord_pin"),
        {"channel_id": channel_id, "message_id": message_id,
         "operation": ("add" if add else "remove") if reaction else
                      ("pin" if add else "unpin"),
         **({"emoji": emoji} if reaction else {})},
        ChangePreview((line, *((ACTION_UNDO_CHANGED,) if changed else ())),
                      recheck, summary=(ACTION_REACTION_LABEL if reaction else ACTION_PIN_LABEL),
                      before={"active": before}),
        run, verify=verify,
        permission=("Add Reactions" if add else "Read Message History") if reaction
                   else "Manage Messages",
    )


async def _prepare(context: AgentRequestContext, arguments: Mapping[str, Any],
                   *, kind: str) -> Mapping[str, Any]:
    await require_evidence_access(context)
    try:
        _, message = await _message(
            context, arguments["channel_id"], arguments["message_id"],
        )
    except DiscordActionRefused as error:
        return {"error": str(error), "prepared_count": 0}
    add = arguments["operation"] in ("add", "pin")
    emoji = arguments.get("emoji", "")
    before = _reaction_active(message, emoji) if kind == "reaction" else message.pinned
    if before == add:
        return {"status": "no_change", "prepared_count": 0}
    context.state.command_proposals.append(_control_action(
        context, channel_id=arguments["channel_id"], message_id=message.id,
        kind=kind, add=add, before=before, emoji=emoji,
        label_message=message.jump_url,
    ))
    return {"status": "confirmation_required"}


async def prepare_reaction(context: AgentRequestContext,
                           arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    return await _prepare(context, arguments, kind="reaction")


async def prepare_pin(context: AgentRequestContext,
                      arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    return await _prepare(context, arguments, kind="pin")


async def prepare_control_undo(context: AgentRequestContext,
                               log: Mapping[str, Any]) -> PreparedAction:
    values = log["targets"]
    kind = "reaction" if "emoji" in values else "pin"
    _, message = await _message(context, values["channel_id"], values["message_id"])
    expected = log["after"]["active"]
    active = (_reaction_active(message, values["emoji"]) if kind == "reaction"
              else message.pinned)
    return _control_action(
        context, channel_id=values["channel_id"], message_id=values["message_id"],
        kind=kind, add=log["before"]["active"], before=expected,
        emoji=values.get("emoji", ""), undo=True, changed=active != expected,
        label_message=message.jump_url,
    )
