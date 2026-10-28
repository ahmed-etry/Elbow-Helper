"""Confirmed native Discord polls and provenance-bound result reads."""

from dataclasses import asdict, dataclass
from datetime import timedelta

import discord

from ..access import require_evidence_access
from ..actions.values import display_value
from .management import ID, complete, guard, queue, state_action, text, tool
from .safety import DiscordActionRefused, resolve_channel


@dataclass(frozen=True)
class PollAnswerRow:
    answer_id: int
    text: str
    votes: int


@dataclass(frozen=True)
class PollSnapshot:
    channel_id: int
    message_id: int
    question: str
    answers: list[PollAnswerRow]
    multiple_choice: bool
    expires_at: str | None
    finalized: bool


def poll_tools():
    return (
        tool("post_discord_poll", "Post a native Discord poll.",
             {"channel_id": ID, "question": text(300), "answers": {
                 "type": "array", "items": text(55), "minItems": 2, "maxItems": 10,
             }, "duration_hours": {"type": "integer", "minimum": 1, "maximum": 768},
              "multiple_choice": {"type": "boolean"}},
             ("channel_id", "question", "answers", "duration_hours"), prepare_poll,
             entities=(("channel_id", "discord_channel"),)),
        tool("read_discord_poll", "Read a visible poll's current answers and vote counts.",
             {"channel_id": ID, "message_id": ID}, ("channel_id", "message_id"), read_poll,
             read=True, entities=(("channel_id", "discord_channel"),
                                  ("message_id", "discord_message"))),
    )


async def post_channel(context, identifier):
    channel = await resolve_channel(context, identifier)
    if not callable(getattr(channel, "send", None)):
        raise DiscordActionRefused("The selected poll is invalid.")
    thread_types = (discord.ChannelType.public_thread, discord.ChannelType.private_thread,
                    discord.ChannelType.news_thread)
    sending = ("send_messages_in_threads" if getattr(channel, "type", None) in thread_types
               else "send_messages")
    guard(context, "view_channel", sending, "send_polls", channel=channel)
    return channel


async def prepare_poll(context, arguments):
    channel = await post_channel(context, arguments["channel_id"])
    if (not 2 <= len(arguments["answers"]) <= 10
            or not 1 <= arguments["duration_hours"] <= 768):
        raise DiscordActionRefused("The selected poll is invalid.")

    async def load():
        await post_channel(context, channel.id)
        return dict(arguments)

    async def apply(reason):
        current = await post_channel(context, channel.id)
        poll = discord.Poll(arguments["question"], timedelta(hours=arguments["duration_hours"]),
                            multiple=arguments.get("multiple_choice", False))
        for answer in arguments["answers"]:
            poll.add_answer(text=answer)
        message = await current.send(poll=poll, allowed_mentions=discord.AllowedMentions.none())
        return complete({"message_id": message.id},
                        result={"message_id": message.id, "channel_id": channel.id})

    hours = arguments["duration_hours"]
    unit = "hour" if hours == 1 else "hours"
    multiple = display_value(arguments.get("multiple_choice", False))
    lines = [f"Post this poll in {channel.mention}:", f"Question: {arguments["question"]}"]
    lines.extend(f"{index}. {answer}" for index, answer in enumerate(arguments["answers"], 1))
    lines.extend((f"Duration: {hours} {unit}", f"Multiple answers: {multiple}"))
    return await queue(context, state_action(
        context, "post_discord_poll", dict(arguments), "Post poll", lines,
        dict(arguments), load, apply,
    ))


async def read_poll(context, arguments):
    await require_evidence_access(context)
    channel = await resolve_channel(context, arguments["channel_id"])
    guard(context, "view_channel", "read_message_history", channel=channel)
    message = await channel.fetch_message(arguments["message_id"])
    if message.poll is None:
        raise DiscordActionRefused("That message does not contain a poll.")
    poll = message.poll
    result = asdict(PollSnapshot(
        channel.id, message.id, poll.question.text,
        [PollAnswerRow(answer.id, answer.text, answer.vote_count) for answer in poll.answers],
        poll.multiple, poll.expires_at.isoformat() if poll.expires_at else None,
        poll.is_finalized(),
    ))
    context.state.source_channels.add(channel.id)
    await require_evidence_access(context)
    return result


async def undo_poll(context, log):
    channel = await resolve_channel(context, log["targets"]["channel_id"])
    identifier = log["after"]["message_id"]

    async def load():
        guard(context, "view_channel", "read_message_history", channel=channel)
        message = await channel.fetch_message(identifier)
        if message.author.id != context.guild.me.id:
            raise DiscordActionRefused("That message is unavailable.")
        return {"message_id": identifier}

    before = await load()

    async def apply(reason):
        await load()
        await (await channel.fetch_message(identifier)).delete()
        return complete({"deleted": True})

    message = await channel.fetch_message(identifier)
    return state_action(context, "undo_poll_post", {"channel_id": channel.id}, "Undo poll post",
                        (f"Delete the poll posted by this change: {message.jump_url}.",),
                        before, load, apply, sources=(channel.id,))


UNDO_HANDLERS = {"post_discord_poll": undo_poll}
