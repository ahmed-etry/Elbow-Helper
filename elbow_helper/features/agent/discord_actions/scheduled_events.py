"""Confirmed guild scheduled events with entity-specific Discord permissions."""

from datetime import datetime, timezone

import discord

from ..actions.values import display_value
from .management import ID, complete, guard, missing_permissions, queue, state_action, text, tool
from .safety import DiscordActionRefused, resolve_channel


KINDS = {"voice": discord.EntityType.voice, "stage": discord.EntityType.stage_instance,
         "external": discord.EntityType.external}
FIELDS = {"name": text(100), "description": text(1000, minimum=0),
          "kind": {"type": "string", "enum": list(KINDS)}, "channel_id": ID,
          "location": text(100), "start_time": text(40), "end_time": text(40)}


def scheduled_event_tools():
    return (
        tool("create_discord_event", "Create a voice, stage or external scheduled event.",
             FIELDS, ("name", "kind", "start_time"), prepare_create,
             entities=(("channel_id", "discord_channel"),)),
        tool("edit_discord_event", "Change an existing scheduled event after confirmation.",
             {"event_id": ID, **FIELDS}, ("event_id",), prepare_edit,
             entities=(("event_id", "discord_event"), ("channel_id", "discord_channel"))),
        tool("cancel_discord_event", "Cancel a scheduled event permanently after confirmation.",
             {"event_id": ID}, ("event_id",), prepare_cancel, irreversible=True,
             entities=(("event_id", "discord_event"),)),
    )


def parsed_time(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return parsed
    except (TypeError, ValueError):
        pass
    raise DiscordActionRefused("The selected event time is invalid.")


def snapshot(event):
    return {"name": event.name, "description": event.description,
            "kind": next(kind for kind, value in KINDS.items() if value == event.entity_type),
            "channel_id": event.channel_id, "location": event.location,
            "start_time": event.start_time.isoformat(),
            "end_time": event.end_time.isoformat() if event.end_time else None,
            "status": event.status.value}


async def resolved(context, identifier):
    try:
        return await context.guild.fetch_scheduled_event(identifier)
    except discord.NotFound as error:
        raise DiscordActionRefused("That scheduled event is unavailable.") from error


async def event_access(context, values, event=None):
    channel = None
    extra = ()
    if values["kind"] != "external":
        if values.get("channel_id") is None:
            raise DiscordActionRefused("The selected event channel is invalid.")
        channel = await resolve_channel(context, values["channel_id"])
        expected = (discord.ChannelType.voice if values["kind"] == "voice"
                    else discord.ChannelType.stage_voice)
        if channel.type != expected:
            raise DiscordActionRefused("The selected event channel is invalid.")
        extra = (("view_channel", "connect") if values["kind"] == "voice" else
                 ("view_channel", "manage_channels", "mute_members", "move_members"))
    asker = context.guild.get_member(context.member.id)
    bot = context.guild.me

    def required(actor):
        if event is None:
            return "create_events"
        if actor is not None and event.creator_id == actor.id and not missing_permissions(
            actor, ("create_events",), channel=channel,
        ):
            return "create_events"
        return "manage_events"

    guard(context, required(asker), *extra, channel=channel,
          bot_permissions=(required(bot), *extra))
    return channel


def validate(values, *, new_start=False):
    start, end = parsed_time(values["start_time"]), parsed_time(values.get("end_time"))
    if new_start and start <= datetime.now(timezone.utc):
        raise DiscordActionRefused("The event must start in the future.")
    if end is not None and end <= start:
        raise DiscordActionRefused("The event must end after it starts.")
    if values["kind"] == "external" and (not values.get("location") or end is None):
        raise DiscordActionRefused("An external event needs a location and an end time.")


async def options(context, values):
    channel = (await resolve_channel(context, values["channel_id"])
               if values["kind"] != "external" else None)
    output = {"name": values["name"], "description": values.get("description"),
              "start_time": parsed_time(values["start_time"]),
              "end_time": parsed_time(values.get("end_time")), "channel": channel,
              "entity_type": KINDS[values["kind"]],
              "privacy_level": discord.PrivacyLevel.guild_only}
    if values["kind"] == "external":
        output["location"] = values["location"]
    return output


async def event_action(context, arguments, *, create=False, undo=False):
    event = None if create else await resolved(context, arguments["event_id"])
    before = dict(arguments) if create else snapshot(event)
    desired = dict(arguments) if create else {**before, **arguments}
    if desired["kind"] == "external":
        desired["channel_id"] = None
    else:
        desired["location"] = None
    validate(desired, new_start=create or "start_time" in arguments)
    await event_access(context, desired, event)
    if event is not None:
        await event_access(context, before, event)

    async def load():
        current = None if create else await resolved(context, event.id)
        validate(desired, new_start=create or "start_time" in arguments)
        await event_access(context, desired, current)
        if current is not None:
            await event_access(context, snapshot(current), current)
        return before if create else snapshot(current)

    async def apply(reason):
        current = None if create else await resolved(context, event.id)
        await event_access(context, desired, current)
        values = await options(context, desired)
        if create:
            if desired["kind"] == "external":
                values.pop("channel")
            if values["description"] is None:
                values.pop("description")
            created = await context.guild.create_scheduled_event(reason=reason, **values)
            return complete({"event_id": created.id}, result={"event_id": created.id})
        mapping = {"entity_type": "kind", "channel": "channel_id"}
        changed_kind = desired["kind"] != before["kind"]
        edits = {key: value for key, value in values.items()
                 if key != "privacy_level" and (
                     desired.get(mapping.get(key, key)) != before.get(mapping.get(key, key))
                     or changed_kind and key in ("entity_type", "channel", "location", "end_time")
                 )}
        await current.edit(reason=reason, **edits)
        return complete(desired)

    line = (f"Create scheduled event **{desired["name"]}**." if create else
            f"Restore the previous settings for scheduled event **{event.name}**." if undo
            else f"Change scheduled event **{event.name}**.")
    labels = {"name": "Name", "description": "Description", "kind": "Type",
              "channel_id": "Channel", "location": "Location",
              "start_time": "Start", "end_time": "End"}
    lines = [line]
    for key, label in labels.items():
        value = desired.get(key)
        if key in ("start_time", "end_time"):
            value = parsed_time(value)
        elif key == "kind":
            value = value.title()
        elif key == "channel_id" and value is not None:
            value = (await resolve_channel(context, value)).mention
        if create:
            lines.append(f"{label}: {display_value(value)}")
        elif before.get(key) != desired.get(key):
            old = before.get(key)
            if key in ("start_time", "end_time"):
                old = parsed_time(old)
            elif key == "kind":
                old = old.title()
            elif key == "channel_id" and old is not None:
                old = (await resolve_channel(context, old)).mention
            lines.append(f"{label}: {display_value(old)} to {display_value(value)}")
    return state_action(context, "create_discord_event" if create else "edit_discord_event",
                        dict(arguments), "Create scheduled event" if create else
                        "Undo scheduled event change" if undo else "Change scheduled event",
                        lines, before, load, apply)


async def prepare_create(context, arguments):
    return await queue(context, await event_action(context, dict(arguments), create=True))


async def prepare_edit(context, arguments):
    return await queue(context, await event_action(context, dict(arguments)))


async def prepare_cancel(context, arguments):
    event = await resolved(context, arguments["event_id"])
    before = snapshot(event)
    await event_access(context, before, event)
    if event.status != discord.EventStatus.scheduled:
        raise DiscordActionRefused("The selected event status is invalid.")

    async def load():
        current = await resolved(context, event.id)
        await event_access(context, snapshot(current), current)
        return snapshot(current)

    async def apply(reason):
        current = await resolved(context, event.id)
        await event_access(context, snapshot(current), current)
        await current.edit(status=discord.EventStatus.canceled, reason=reason)
        return complete({"canceled": True})

    return await queue(context, state_action(
        context, "cancel_discord_event", dict(arguments), "Cancel scheduled event",
        (f"Cancel scheduled event **{event.name}**.",), before, load, apply, irreversible=True,
    ))


async def undo_edit(context, log):
    arguments = {"event_id": log["targets"]["event_id"]}
    arguments.update({key: log["before"].get(key) for key in FIELDS
                      if log["before"].get(key) != log["after"].get(key)})
    return await event_action(context, arguments, undo=True)


async def undo_create(context, log):
    identifier = log["after"]["event_id"]
    event = await resolved(context, identifier)

    async def load():
        current = await resolved(context, identifier)
        await event_access(context, snapshot(current), current)
        if current.status != discord.EventStatus.scheduled:
            raise DiscordActionRefused("The selected event status is invalid.")
        return snapshot(current)

    before = await load()

    async def apply(reason):
        await load()
        await (await resolved(context, identifier)).delete(reason=reason)
        return complete({"deleted": True})

    return state_action(context, "undo_scheduled_event_creation", {"event_id": identifier},
                        "Undo scheduled event creation",
                        (f"Delete the scheduled event created by this change: **{event.name}**.",),
                        before, load, apply)


UNDO_HANDLERS = {"create_discord_event": undo_create, "edit_discord_event": undo_edit}
