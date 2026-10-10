"""Confirmed text channel and category management with saved settings and guarded undo."""

import discord

from ..actions.values import display_value
from .channel_permissions import (
    channel_permission_tools, check_overwrite_grants, desired_overwrite, overwrite_lines,
    resolve_targets, unpacked,
)
from .management import ID, complete, guard, queue, state_action, text, tool
from .safety import DiscordActionRefused, resolve_channel


NULL_ID = {"type": ["integer", "null"], "minimum": 1}
SETTINGS = {
    "name": text(100), "category_id": NULL_ID, "topic": text(1024, minimum=0),
    "slowmode_seconds": {"type": "integer", "minimum": 0, "maximum": 21600},
}


def channel_tools():
    overwrites = channel_permission_tools()[0].definition.parameters["properties"]["targets"]
    return (
        tool("create_discord_channel", "Create a text channel or category after confirmation.",
             {"kind": {"type": "string", "enum": ["text", "category"]}, **SETTINGS,
              "overwrites": overwrites}, ("kind", "name"), prepare_create,
             entities=(("category_id", "discord_channel"),)),
        tool("edit_discord_channel", "Rename, move or change topic and slowmode after confirmation.",
             {"channel_id": ID, **SETTINGS}, ("channel_id",), prepare_edit,
             entities=(("channel_id", "discord_channel"),
                       ("category_id", "discord_channel"))),
        tool("delete_discord_channel", "Delete a reviewed text channel or category permanently.",
             {"channel_id": ID}, ("channel_id",), prepare_delete, irreversible=True,
             entities=(("channel_id", "discord_channel"),)),
    )


def duration(seconds):
    for unit, size in (("hour", 3600), ("minute", 60), ("second", 1)):
        if seconds and seconds % size == 0:
            amount = seconds // size
            return f"{amount} {unit}{'' if amount == 1 else 's'}"
    return "0 seconds"


async def category(context, identifier):
    if identifier is None:
        guard(context, "manage_channels")
        return None
    parent = await resolve_channel(context, identifier)
    if parent.type != discord.ChannelType.category:
        raise DiscordActionRefused("The selected category is invalid.")
    guard(context, "view_channel", "manage_channels", channel=parent)
    return parent


async def editable(context, identifier):
    channel = await resolve_channel(context, identifier)
    if channel.type not in (discord.ChannelType.text, discord.ChannelType.category):
        raise DiscordActionRefused("That channel is unavailable.")
    guard(context, "view_channel", "manage_channels", channel=channel)
    return channel


def settings(channel):
    return {"name": channel.name, "category_id": getattr(channel, "category_id", None),
            "topic": getattr(channel, "topic", None),
            "slowmode_seconds": getattr(channel, "slowmode_delay", 0)}


async def initial_overwrites(context, parent, specifications):
    if not specifications:
        return {}, []
    if parent is None:
        guard(context, "manage_roles")
        parent = context.source_message.channel
        # A root channel has guild permission semantics, not the request channel's overwrites.
        class RootPermissions:
            mention = "this server"

            def permissions_for(self, actor):
                return actor.guild_permissions

        parent = RootPermissions()
    selected = await resolve_targets(context, parent, specifications)
    overwrites, lines = {}, []
    for target, spec in zip(selected, specifications):
        after = desired_overwrite(None, spec)
        check_overwrite_grants(context, parent, None, after)
        if after is not None:
            overwrites[target] = unpacked(after)
        lines.extend(overwrite_lines(target, parent, None, after))
    return overwrites, lines


async def create_action(context, arguments):
    if arguments["kind"] == "category" and any(
        key in arguments for key in ("category_id", "topic", "slowmode_seconds")
    ):
        raise DiscordActionRefused("The selected category is invalid.")
    parent = await category(context, arguments.get("category_id"))
    _, permission_lines = await initial_overwrites(context, parent, arguments.get("overwrites", []))

    async def load():
        parent = await category(context, arguments.get("category_id"))
        await initial_overwrites(context, parent, arguments.get("overwrites", []))
        return dict(arguments)

    async def apply(reason):
        parent = await category(context, arguments.get("category_id"))
        overwrites, _ = await initial_overwrites(context, parent, arguments.get("overwrites", []))
        options = {"overwrites": overwrites} if "overwrites" in arguments else {}
        if arguments["kind"] == "category":
            created = await context.guild.create_category(
                arguments["name"], reason=reason, **options,
            )
        else:
            created = await context.guild.create_text_channel(
                arguments["name"], category=parent, topic=arguments.get("topic"),
                slowmode_delay=arguments.get("slowmode_seconds", 0), reason=reason, **options,
            )
        return complete({"channel_id": created.id}, result={"channel_id": created.id})

    kind = "text channel" if arguments["kind"] == "text" else "category"
    place = parent.mention if parent is not None else "No category"
    return state_action(
        context, "create_discord_channel", dict(arguments),
        "Create text channel" if arguments["kind"] == "text" else "Create category",
        [f"Create {kind} **{arguments["name"]}** in {place}.", *permission_lines],
        dict(arguments), load, apply,
    )


async def edit_action(context, arguments, *, undo=False):
    channel = await editable(context, arguments["channel_id"])
    before = settings(channel)
    desired = {**before, **{key: arguments[key] for key in SETTINGS if key in arguments}}
    if channel.type == discord.ChannelType.category and any(
        key in arguments for key in ("category_id", "topic", "slowmode_seconds")
    ):
        raise DiscordActionRefused("The selected category is invalid.")
    moving = desired["category_id"] != before["category_id"]
    if moving:
        await category(context, desired["category_id"])

    async def load():
        current = await editable(context, channel.id)
        if moving:
            await category(context, desired["category_id"])
        return settings(current)

    async def apply(reason):
        current = await editable(context, channel.id)
        parent = await category(context, desired["category_id"]) if moving else current.category
        options = {"name": desired["name"]}
        if channel.type == discord.ChannelType.text:
            options.update(category=parent, sync_permissions=False, topic=desired["topic"],
                           slowmode_delay=desired["slowmode_seconds"])
        await current.edit(reason=reason, **options)
        return complete(desired)

    labels = {"name": "Name", "category_id": "Category", "topic": "Topic",
              "slowmode_seconds": "Slowmode"}

    async def visible(key, value):
        if key == "category_id":
            return ((await resolve_channel(context, value)).mention
                    if value is not None else "No category")
        return duration(value) if key == "slowmode_seconds" else display_value(value)

    lines = [f"Restore the previous settings for {channel.mention}." if undo
             else f"Change {channel.mention}."]
    for key in labels:
        if before[key] != desired[key]:
            lines.append(f"{labels[key]}: {await visible(key, before[key])} to "
                         f"{await visible(key, desired[key])}")
    return state_action(context, "edit_discord_channel", dict(arguments),
                        "Undo channel change" if undo else "Change channel",
                        lines, before, load, apply)


async def prepare_create(context, arguments):
    return await queue(context, await create_action(context, dict(arguments)))


async def prepare_edit(context, arguments):
    return await queue(context, await edit_action(context, dict(arguments)))


async def prepare_delete(context, arguments):
    channel = await editable(context, arguments["channel_id"])
    before = settings(channel)
    if channel.type == discord.ChannelType.category:
        estimate = f"Channels in this category: {len(channel.channels)}"
    else:
        guard(context, "read_message_history", channel=channel)
        count = sum([1 async for _ in channel.history(limit=1001)])
        estimate = f"Messages: {'at least' if count == 1001 else 'about'} {count}"

    async def load():
        return settings(await editable(context, channel.id))

    async def apply(reason):
        await (await editable(context, channel.id)).delete(reason=reason)
        return complete({"deleted": True})

    parent = channel.category.mention if channel.category else "No category"
    return await queue(context, state_action(
        context, "delete_discord_channel", dict(arguments), "Delete channel",
        (f"Delete {channel.mention}.", f"Category: {parent}", estimate),
        before, load, apply, irreversible=True,
    ))


async def undo_edit(context, log):
    arguments = {"channel_id": log["targets"]["channel_id"], **log["before"]}
    channel = await editable(context, arguments["channel_id"])
    if channel.type == discord.ChannelType.category:
        arguments = {"channel_id": channel.id, "name": arguments["name"]}
    return await edit_action(context, arguments, undo=True)


async def undo_create(context, log):
    identifier = log["after"]["channel_id"]

    async def load():
        channel = await editable(context, identifier)
        if channel.type == discord.ChannelType.category:
            if channel.channels:
                raise DiscordActionRefused(
                    "That category contains channels, so it cannot be deleted by undo."
                )
        else:
            guard(context, "read_message_history", channel=channel)
            async for message in channel.history(limit=None):
                if not message.author.bot:
                    raise DiscordActionRefused(
                        "A member has posted in that channel, so it cannot be deleted by undo."
                    )
        return {"channel_id": identifier}

    before = await load()

    async def apply(reason):
        await load()
        await (await editable(context, identifier)).delete(reason=reason)
        return complete({"deleted": True})

    channel = await editable(context, identifier)
    return state_action(context, "undo_channel_creation", {"channel_id": identifier},
                        "Undo channel creation",
                        (f"Delete the channel created by this change: {channel.mention}.",),
                        before, load, apply)


UNDO_HANDLERS = {"create_discord_channel": undo_create, "edit_discord_channel": undo_edit}
