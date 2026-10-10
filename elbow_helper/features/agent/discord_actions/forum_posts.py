"""Confirmed forum posts with existing tags and reply-sensitive creation undo."""

import discord

from .management import ID, complete, guard, queue, state_action, text, tool
from .safety import DiscordActionRefused, resolve_channel


def forum_tools():
    return (tool(
        "create_discord_forum_post", "Create a forum post with existing tags after confirmation.",
        {"channel_id": ID, "title": text(100), "content": text(2000), "tag_ids": {
            "type": "array", "items": ID, "maxItems": 5, "uniqueItems": True,
        }}, ("channel_id", "title", "content"), prepare_post,
        entities=(("channel_id", "discord_channel"),),
    ),)


async def forum(context, identifier, tag_ids):
    channel = await resolve_channel(context, identifier)
    if channel.type != discord.ChannelType.forum:
        raise DiscordActionRefused("The selected forum channel is invalid.")
    guard(context, "view_channel", "send_messages", channel=channel)
    available = {tag.id: tag for tag in channel.available_tags}
    missing = [identifier for identifier in tag_ids if identifier not in available]
    if missing:
        ids = ", ".join(map(str, missing))
        raise DiscordActionRefused(f"These tags are unavailable in {channel.mention}: {ids}.")
    if (len(tag_ids) > 5 or len(set(tag_ids)) != len(tag_ids)
            or channel.flags.require_tag and not tag_ids):
        raise DiscordActionRefused("The selected forum tag is invalid.")
    selected = [available[identifier] for identifier in tag_ids]
    if any(tag.moderated for tag in selected):
        guard(context, "manage_threads", channel=channel)
    return channel, selected


async def prepare_post(context, arguments):
    identifiers = arguments.get("tag_ids", [])
    channel, tags = await forum(context, arguments["channel_id"], identifiers)

    async def load():
        await forum(context, channel.id, identifiers)
        return dict(arguments)

    async def apply(reason):
        current, selected = await forum(context, channel.id, identifiers)
        created = await current.create_thread(
            name=arguments["title"], content=arguments["content"], applied_tags=selected,
            allowed_mentions=discord.AllowedMentions.none(), reason=reason,
        )
        return complete({"thread_id": created.thread.id, "message_id": created.message.id},
                        result={"thread_id": created.thread.id, "message_id": created.message.id})

    labels = ", ".join(tag.name for tag in tags) or "Not set"
    return await queue(context, state_action(
        context, "create_discord_forum_post", dict(arguments), "Create forum post",
        (f"Create forum post **{arguments["title"]}** in {channel.mention}.",
         "Content:", arguments["content"], f"Tags: {labels}"), dict(arguments), load, apply,
    ))


async def undo_post(context, log):
    channel_id = log["targets"]["channel_id"]
    identifier = log["after"]["thread_id"]

    async def load():
        parent = await resolve_channel(context, channel_id)
        guard(context, "view_channel", "read_message_history", "manage_threads", channel=parent)
        thread = await resolve_channel(context, identifier)
        if thread.parent_id != channel_id or thread.owner_id != context.guild.me.id:
            raise DiscordActionRefused("That forum post is unavailable.")
        async for message in thread.history(limit=None):
            if message.id != log["after"]["message_id"] and not message.author.bot:
                raise DiscordActionRefused(
                    "A member has replied to that post, so it cannot be deleted by undo."
                )
        return {"thread_id": identifier}

    before = await load()

    async def apply(reason):
        await load()
        await (await resolve_channel(context, identifier)).delete(reason=reason)
        return complete({"deleted": True})

    thread = await resolve_channel(context, identifier)
    return state_action(context, "undo_forum_post", {"thread_id": identifier}, "Undo forum post",
                        (f"Delete the forum post created by this change: {thread.mention}.",),
                        before, load, apply)


UNDO_HANDLERS = {"create_discord_forum_post": undo_post}
