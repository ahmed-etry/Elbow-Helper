"""Exact channel permission overwrite previews and reversible changes."""

import discord

from .management import (
    ID, check_grants, complete, guard, hierarchy, permission_name, permission_values,
    queue, state_action, tool,
)
from .safety import DiscordActionRefused, resolve_channel, resolve_member


def channel_permission_tools():
    return (tool(
        "change_channel_permissions", "Set, change or remove channel permission overwrites.",
        {"channel_id": ID, "targets": {
            "type": "array", "minItems": 1, "maxItems": 25,
            "items": {"type": "object", "additionalProperties": False, "properties": {
                "target_id": ID, "target_type": {"type": "string", "enum": ["role", "member"]},
                "operation": {"type": "string", "enum": ["set", "change", "remove"]},
                "permissions": {"type": "object", "additionalProperties": {
                    "type": ["boolean", "null"],
                }},
            }, "required": ["target_id", "target_type", "operation"]},
        }}, ("channel_id", "targets"), prepare_permissions,
        entities=(("channel_id", "discord_channel"),),
    ),)


def packed(overwrite):
    if overwrite is None:
        return None
    allow, deny = overwrite.pair()
    return {"allow": allow.value, "deny": deny.value}


def unpacked(value):
    if value is None:
        return None
    return discord.PermissionOverwrite.from_pair(
        discord.Permissions(value["allow"]), discord.Permissions(value["deny"]),
    )


async def resolve_targets(context, channel, specifications):
    guard(context, "view_channel", "manage_roles", channel=channel)
    targets = []
    identities = set()
    for spec in specifications:
        identity = (spec["target_type"], spec["target_id"])
        if identity in identities:
            raise DiscordActionRefused("The selected permission target is invalid.")
        identities.add(identity)
        target = (context.guild.get_role(spec["target_id"]) if spec["target_type"] == "role"
                  else await resolve_member(context.guild, spec["target_id"], fresh=True))
        if not (spec["target_type"] == "role" and target is not None
                and target.id == context.guild.id):
            hierarchy(context, target, member=spec["target_type"] == "member")
        targets.append(target)
    return targets


def desired_overwrite(before, spec):
    if spec["operation"] == "remove":
        return None
    values = spec.get("permissions", {})
    permission_values({name: False if value is None else value
                       for name, value in values.items()})
    overwrite = (unpacked(before) if spec["operation"] == "change" and before is not None
                 else discord.PermissionOverwrite())
    overwrite.update(**values)
    return packed(overwrite)


def check_overwrite_grants(context, channel, before, after):
    previous = before or {"allow": 0, "deny": 0}
    desired = after or {"allow": 0, "deny": 0}
    granted = desired["allow"] | (previous["deny"] & ~desired["deny"])
    check_grants(context, discord.Permissions(granted), channel=channel)


def overwrite_lines(target, channel, before, after):
    lines = [f"Change permissions for {target.mention} in {channel.mention}."]
    if after is None:
        lines[0] = f"Remove the permission overwrite for {target.mention} in {channel.mention}."
    old, new = unpacked(before), unpacked(after)
    names = sorted({name for overwrite in (old, new) if overwrite is not None
                    for name, value in overwrite if value is not None})
    labels = {True: "Allow", False: "Deny", None: "Inherit"}
    for name in names:
        left = getattr(old, name) if old is not None else None
        right = getattr(new, name) if new is not None else None
        lines.append(f"{permission_name(name, channel=True)}: {labels[left]} to {labels[right]}")
    if not names:
        left = "No permission overwrite" if before is None else "No permissions"
        right = "No permission overwrite" if after is None else "No permissions"
        lines.extend((f"Before: {left}", f"After: {right}"))
    return lines


async def permission_action(context, values, desired=None, *, undo=False):
    channel = await resolve_channel(context, values["channel_id"])
    specifications = values["targets"]
    targets = await resolve_targets(context, channel, specifications)
    before = [packed(channel.overwrites.get(target)) for target in targets]
    after = desired if undo else [desired_overwrite(old, spec)
                                 for old, spec in zip(before, specifications)]
    for old, new in zip(before, after):
        check_overwrite_grants(context, channel, old, new)

    async def load():
        current = await resolve_channel(context, channel.id)
        selected = await resolve_targets(context, current, specifications)
        for old, new in zip(before, after):
            check_overwrite_grants(context, current, old, new)
        return [packed(current.overwrites.get(target)) for target in selected]

    async def apply(reason):
        current = await resolve_channel(context, channel.id)
        selected = await resolve_targets(context, current, specifications)
        for target, old, new in zip(selected, before, after):
            check_overwrite_grants(context, current, old, new)
            await current.set_permissions(target, overwrite=unpacked(new), reason=reason)
        return complete(after)

    lines = [line for target, old, new in zip(targets, before, after)
             for line in overwrite_lines(target, channel, old, new)]
    return state_action(
        context, "change_channel_permissions", values,
        "Undo channel permissions" if undo else "Change channel permissions",
        lines, before, load, apply,
    )


async def prepare_permissions(context, arguments):
    return await queue(context, await permission_action(context, dict(arguments)))


async def undo_permissions(context, log):
    return await permission_action(context, log["targets"], log["before"], undo=True)


UNDO_HANDLERS = {"change_channel_permissions": undo_permissions}
