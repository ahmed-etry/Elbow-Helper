"""Confirmed member timeouts with hierarchy checks and a Discord-bounded deadline."""

from datetime import datetime, timedelta, timezone

from ..actions.contracts import audit_reason
from ..actions.values import display_value
from .management import ID, complete, guard, hierarchy, queue, state_action, text, tool
from .safety import DiscordActionRefused, resolve_member


MAX_TIMEOUT_SECONDS = 28 * 24 * 60 * 60


def timeout_tools():
    return (tool(
        "change_discord_timeout", "Time out a member for up to 28 days, or remove their timeout.",
        {"member_id": ID, "operation": {"type": "string", "enum": ["set", "remove"]},
         "duration_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_SECONDS},
         "reason": text(400, minimum=0)}, ("member_id", "operation"), prepare_timeout,
        entities=(("member_id", "discord_member"),),
    ),)


async def target(context, identifier, until):
    guard(context, "moderate_members")
    member = await resolve_member(context.guild, identifier, fresh=True)
    hierarchy(context, member, member=True)
    if until is not None:
        if member.guild_permissions.administrator:
            raise DiscordActionRefused("Members with Administrator cannot be timed out.")
        now = datetime.now(timezone.utc)
        if not now < until <= now + timedelta(seconds=MAX_TIMEOUT_SECONDS):
            raise DiscordActionRefused("A timeout must be between 1 second and 28 days.")
    return member


def current_timeout(member):
    value = member.communication_disabled_until
    return value.isoformat() if value else None


async def timeout_action(context, arguments, until, *, undo=False):
    member = await target(context, arguments["member_id"], until)
    given_reason = arguments.get("reason", "")
    reason = audit_reason(context.member)
    if len(f"{given_reason}; {reason}" if given_reason else reason) > 512:
        raise DiscordActionRefused("The selected reason is invalid.")
    before = {"until": current_timeout(member)}

    async def load():
        return {"until": current_timeout(await target(context, member.id, until))}

    async def apply(reason):
        current = await target(context, member.id, until)
        if len(f"{given_reason}; {reason}" if given_reason else reason) > 512:
            raise DiscordActionRefused("The selected reason is invalid.")
        await current.timeout(until, reason=f"{given_reason}; {reason}" if given_reason else reason)
        return complete({"until": until.isoformat() if until else None})

    line = (f"Time out {member.mention} until {display_value(until)}." if until else
            f"Remove the timeout from {member.mention}.")
    if undo and until is None:
        line = f"Remove the timeout set by this change from {member.mention}."
    lines = [line]
    if arguments.get("reason"):
        lines.append(f"Reason: {arguments["reason"]}")
    return state_action(
        context, "change_discord_timeout", dict(arguments),
        "Undo timeout" if undo else "Set timeout" if until else "Remove timeout",
        lines, before, load, apply,
    )


async def prepare_timeout(context, arguments):
    until = None
    if arguments["operation"] == "set":
        seconds = arguments.get("duration_seconds")
        if type(seconds) is not int or not 1 <= seconds <= MAX_TIMEOUT_SECONDS:
            raise DiscordActionRefused("A timeout must be between 1 second and 28 days.")
        until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return await queue(context, await timeout_action(context, dict(arguments), until))


async def undo_timeout(context, log):
    until = None
    if log["after"]["until"] is None and log["before"]["until"]:
        old = datetime.fromisoformat(log["before"]["until"])
        until = old if old > datetime.now(timezone.utc) else None
    return await timeout_action(context, log["targets"], until, undo=True)


UNDO_HANDLERS = {"change_discord_timeout": undo_timeout}
