"""Reversible creation and settings changes for roles below both actors."""

import discord

from ..actions.values import display_value
from .management import (
    ID, PERMISSIONS, check_grants, complete, guard, hierarchy, permission_name,
    permission_values, queue, state_action, text, tool,
)
from .safety import DiscordActionRefused


FIELDS = {
    "name": text(100), "colour": {"type": "integer", "minimum": 0, "maximum": 0xFFFFFF},
    "hoist": {"type": "boolean"}, "mentionable": {"type": "boolean"},
    "position": {"type": "integer", "minimum": 1}, "permissions": PERMISSIONS,
}


def role_settings_tools():
    return (
        tool("create_discord_role", "Create a role below both actors after confirmation.",
             FIELDS, ("name",), prepare_create),
        tool("edit_discord_role", "Change a role's settings and permissions after confirmation.",
             {"role_id": ID, **FIELDS}, ("role_id",), prepare_edit,
             entities=(("role_id", "discord_role"),)),
    )


def snapshot(role):
    return {"name": role.name, "colour": role.colour.value, "hoist": role.hoist,
            "mentionable": role.mentionable, "position": role.position,
            "permissions": role.permissions.value}


def resolved(context, identifier):
    guard(context, "manage_roles")
    role = context.guild.get_role(identifier)
    if role is None:
        raise DiscordActionRefused("That role is unavailable.")
    if role.id == context.guild.id or role.is_default():
        raise DiscordActionRefused("The everyone role cannot be changed.")
    if role.managed:
        raise DiscordActionRefused("An integration manages that role.")
    hierarchy(context, role)
    return role


def check_position(context, position):
    asker, bot = guard(context, "manage_roles")
    if not 1 <= position < min(asker.top_role.position, bot.top_role.position):
        raise DiscordActionRefused("That role must be below your highest role and the bot's.")


def visible(key, value):
    if key == "colour":
        return f"#{value:06X}"
    if key == "permissions":
        names = [permission_name(name) for name, enabled in discord.Permissions(value) if enabled]
        return ", ".join(names) or "No permissions"
    return display_value(value)


def desired_settings(context, arguments, before):
    desired = {**before, **{key: value for key, value in arguments.items() if key in FIELDS}}
    if "permissions" in arguments:
        updates = permission_values(arguments["permissions"])
        permissions = discord.Permissions(before["permissions"])
        permissions.update(**arguments["permissions"])
        if updates.administrator:
            check_grants(context, updates)
        check_grants(context, discord.Permissions(permissions.value & ~before["permissions"]))
        desired["permissions"] = permissions.value
    check_position(context, desired["position"])
    return desired


async def role_action(context, arguments, *, create=False, undo=False):
    guard(context, "manage_roles")
    role = None if create else resolved(context, arguments["role_id"])
    before = ({"name": "", "colour": 0, "hoist": False, "mentionable": False,
               "position": 1, "permissions": 0} if create else snapshot(role))
    desired = desired_settings(context, arguments, before)

    async def load():
        guard(context, "manage_roles")
        desired_settings(context, arguments, before)
        return before if create else snapshot(resolved(context, role.id))

    async def apply(reason):
        guard(context, "manage_roles")
        desired_settings(context, arguments, before)
        if create:
            created = await context.guild.create_role(
                name=desired["name"], colour=discord.Colour(desired["colour"]),
                hoist=desired["hoist"], mentionable=desired["mentionable"],
                permissions=discord.Permissions(desired["permissions"]), reason=reason,
            )
            await created.edit(position=desired["position"], reason=reason)
            return complete({**desired, "role_id": created.id}, result={"role_id": created.id})
        current = resolved(context, role.id)
        options = {key: desired[key] for key in FIELDS if before[key] != desired[key]}
        if "permissions" in options:
            options["permissions"] = discord.Permissions(options["permissions"])
        if "colour" in options:
            options["colour"] = discord.Colour(options["colour"])
        await current.edit(reason=reason, **options)
        return complete(desired)

    line = (f"Create role **{desired["name"]}**." if create else
            f"Restore the previous settings for {role.mention}." if undo
            else f"Change {role.mention}.")
    lines = [line]
    for key in FIELDS:
        if create or before[key] != desired[key]:
            lines.append(f"{key.title()}: {visible(key, before[key])} to "
                         f"{visible(key, desired[key])}")
    return state_action(
        context, "create_discord_role" if create else "edit_discord_role", dict(arguments),
        "Create role" if create else "Undo role settings" if undo else "Change role",
        lines, before, load, apply,
    )


async def prepare_create(context, arguments):
    return await queue(context, await role_action(context, dict(arguments), create=True))


async def prepare_edit(context, arguments):
    return await queue(context, await role_action(context, dict(arguments)))


async def undo_edit(context, log):
    arguments = {"role_id": log["targets"]["role_id"]}
    for key in FIELDS:
        if log["before"][key] != log["after"][key]:
            arguments[key] = log["before"][key]
    if "permissions" in arguments:
        arguments["permissions"] = dict(discord.Permissions(arguments["permissions"]))
    return await role_action(context, arguments, undo=True)


async def undo_create(context, log):
    identifier = log["after"]["role_id"]

    async def load():
        role = resolved(context, identifier)
        for member in role.members:
            hierarchy(context, member, member=True)
        return {**snapshot(role), "members": sorted(member.id for member in role.members)}

    before = await load()

    async def apply(reason):
        await load()
        await resolved(context, identifier).delete(reason=reason)
        return complete({"deleted": True})

    role = resolved(context, identifier)
    return state_action(context, "undo_role_creation", {"role_id": identifier},
                        "Undo role creation",
                        (f"Delete the role created by this change: {role.mention}.",),
                        before, load, apply)


UNDO_HANDLERS = {"create_discord_role": undo_create, "edit_discord_role": undo_edit}
