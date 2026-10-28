"""Shared contracts, permission guards and state checks for Discord management actions."""

from __future__ import annotations

import discord

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass, ChangePreview, PreparedAction, audit_reason
from ..actions.outcomes import ActionOutcome
from ..engine.capability_contract import CapabilityContract
from ..models import AgentCapabilityEffect, RegisteredAgentTool
from .permission_checks import below_both_roles, missing_permissions, unheld_permissions
from .safety import DiscordActionRefused


ID = {"type": "integer", "minimum": 1}
PERMISSIONS = {"type": "object", "additionalProperties": {"type": "boolean"}}


def text(maximum, *, minimum=1):
    return {"type": "string", "minLength": minimum, "maxLength": maximum}


def tool(name, description, properties, required, handler, *, entities=(),
         irreversible=False, read=False, required_access=frozenset()):
    return RegisteredAgentTool(
        AgentToolDefinition(name, description, {
            "type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False,
        }), handler,
        AgentCapabilityEffect.READ if read else AgentCapabilityEffect.COMMAND,
        ActionClass.READ if read else (
            ActionClass.IRREVERSIBLE if irreversible else ActionClass.CHANGE
        ),
        contract=CapabilityContract(
            entity_fields=entities, source_scope="request_context",
            filter_fields=tuple(key for key in properties if key not in dict(entities)),
            required_access=required_access,
        ),
    )


def permission_name(name, *, channel=False):
    if name == "manage_roles" and channel:
        return "Manage Permissions"
    labels = {
        "read_messages": "View Channel", "manage_guild": "Manage Server",
        "create_instant_invite": "Create Invite", "stream": "Video",
        "external_emojis": "Use External Emojis", "external_stickers": "Use External Stickers",
        "use_voice_activation": "Use Voice Activity", "manage_expressions": "Manage Expressions",
        "use_embedded_activities": "Use Activities", "moderate_members": "Moderate Members",
        "mention_everyone": "Mention Everyone",
        "use_application_commands": "Use Application Commands",
    }
    return labels.get(name, name.replace("_", " ").title())


def guard(context, *permissions, channel=None, bot_permissions=None):
    requester = context.guild.get_member(context.member.id)
    bot = context.guild.me
    place = channel.mention if channel is not None else "this server"
    for actor, subject, required in (
        (requester, "You need", permissions),
        (bot, "The bot needs", permissions if bot_permissions is None else bot_permissions),
    ):
        missing = missing_permissions(actor, required, channel=channel)
        if missing:
            labels = ", ".join(permission_name(name, channel=channel is not None)
                               for name in missing)
            raise DiscordActionRefused(f"{subject} {labels} in {place}.")
    return requester, bot


def hierarchy(context, target, *, member=False):
    requester = context.guild.get_member(context.member.id)
    bot = context.guild.me
    role = getattr(target, "top_role", None) if member else target
    if (target is None or not below_both_roles(role, requester, bot)
            or member and target.id in (context.guild.owner_id, context.member.id, bot.id)):
        kind = "member" if member else "role"
        raise DiscordActionRefused(
            f"That {kind} must be below your highest role and the bot's."
        )


def permission_values(values):
    invalid = sorted(name for name, value in values.items()
                     if name not in discord.Permissions.VALID_FLAGS or type(value) is not bool)
    if invalid:
        raise DiscordActionRefused(f"These permissions are invalid: {', '.join(invalid)}.")
    return discord.Permissions(**values)


def check_grants(context, granted, *, channel=None):
    if granted.administrator:
        raise DiscordActionRefused("Administrator cannot be granted.")
    requester, bot = guard(context, channel=channel)
    for actor in (requester, bot):
        missing = unheld_permissions(granted, actor, channel=channel)
        if missing:
            labels = ", ".join(permission_name(name, channel=channel is not None)
                               for name in missing)
            subject = "You cannot grant" if actor is requester else "The bot needs"
            if actor is requester:
                raise DiscordActionRefused(f"{subject} these permissions: {labels}.")
            place = channel.mention if channel is not None else "this server"
            raise DiscordActionRefused(f"{subject} {labels} in {place}.")


async def queue(context, action):
    await require_evidence_access(context)
    context.state.proposed_changes.append(action)
    return {"status": "confirmation_required"}


def state_action(context, path, values, summary, lines, before, load, apply, *,
                 irreversible=False, sources=(), details=()):
    async def recheck():
        await require_evidence_access(context)
        return await load() == before

    async def run():
        await require_evidence_access(context)
        if await load() != before:
            raise DiscordActionRefused("This has changed since then.")
        return await apply(audit_reason(context.member))

    return PreparedAction(
        path, values, ChangePreview(
            tuple(lines), recheck, summary=summary, before=before,
            details=tuple(details), detail_sources=frozenset(sources),
        ), run, ActionClass.IRREVERSIBLE if irreversible else ActionClass.CHANGE,
    )


def complete(after, *, text="", result=None):
    return ActionOutcome("complete", text=text, after=after, result=result)
