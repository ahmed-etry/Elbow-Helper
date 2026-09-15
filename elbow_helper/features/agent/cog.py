"""Mention-driven Discord surface for the Core agent beta."""

from __future__ import annotations

import asyncio
import io
import logging
import re

import discord
from discord.ext import commands

from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import CORE
from elbow_helper.discord.interactions import DEFAULT_FAILURE_MESSAGE

from .models import AgentRequestContext
from .message_content import message_text
from .access import AgentAccessLost, require_access
from .service import AgentUnavailableError
from .service import CoreAgentService


LOGGER = logging.getLogger(__name__)
LOCAL_CONTEXT_MESSAGES = 8
LOCAL_MESSAGE_CHARACTER_LIMIT = 1_200
AGENT_REQUEST_TIMEOUT_SECONDS = 240.0
AGENT_CONCURRENCY = 4
DISCORD_MESSAGE_LIMIT = 2_000
MAX_RESPONSE_CHARACTERS = 12_000


class CoreAgent(commands.Cog):
    """Answer direct mentions from Core members without changing server state."""

    def __init__(
        self,
        bot: commands.Bot,
        *,
        account_links,
        clan_health,
        message_search,
    ):
        self.bot = bot
        self.account_links = account_links
        self.clan_health = clan_health
        self.message_search = message_search
        self.service = CoreAgentService(bot.agent_model)
        self._active_members: set[int] = set()
        self._tasks: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(AGENT_CONCURRENCY)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self._is_agent_request(message):
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        if not any(role.id in CORE for role in member.roles):
            return
        question = self._extract_question(message)
        if not question or member.id in self._active_members:
            return

        self._active_members.add(member.id)
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            async with asyncio.timeout(AGENT_REQUEST_TIMEOUT_SECONDS):
                async with self._semaphore:
                    await self._answer(message, member, question)
        except AgentAccessLost:
            LOGGER.info("Agent access lost: invoker=%s", member.id)
        except TimeoutError:
            await self._send_failure(message)
        except (discord.DiscordException, OSError, RuntimeError, TypeError, ValueError):
            LOGGER.exception("Agent request failed: invoker=%s", member.id)
            await self._send_failure(message)
        finally:
            self._active_members.discard(member.id)
            self._tasks.discard(task)

    def cog_unload(self):
        for task in tuple(self._tasks):
            task.cancel()

    def _is_agent_request(self, message: discord.Message) -> bool:
        bot_user = self.bot.user
        return bool(
            bot_user is not None
            and message.guild is not None
            and message.guild.id == GUILD_ID
            and not message.author.bot
            and not getattr(message, "webhook_id", None)
            and bot_user.id in message.raw_mentions
        )

    def _extract_question(self, message: discord.Message) -> str:
        bot_user = self.bot.user
        if bot_user is None:
            return ""
        mention_pattern = re.compile(rf"<@!?{bot_user.id}>")
        return mention_pattern.sub("", message.content or "").strip()

    async def _answer(
        self,
        message: discord.Message,
        member: discord.Member,
        question: str,
    ) -> None:
        member = require_access(message.guild, member.id, message.channel)
        referenced = await self._resolve_referenced_message(message)
        local_context = await self._build_local_context(message, referenced)
        context = AgentRequestContext(
            bot=self.bot,
            guild=message.guild,
            member=member,
            source_message=message,
            account_links=self.account_links,
            clan_health=self.clan_health,
            message_search=self.message_search,
        )

        try:
            async with message.channel.typing():
                response = await self.service.answer(
                    question=question,
                    local_context=local_context,
                    context=context,
                )
            require_access(message.guild, member.id, message.channel)
            await self._send_response(message, response, referenced)
        except AgentAccessLost:
            raise
        except AgentUnavailableError as error:
            LOGGER.warning(
                "Core agent unavailable: invoker=%s channel=%s error=%s",
                member.id,
                message.channel.id,
                error,
            )
            await self._send_failure(message)
        except asyncio.TimeoutError:
            LOGGER.warning(
                "Core agent timed out: invoker=%s channel=%s",
                member.id,
                message.channel.id,
            )
            await self._send_failure(message)
        except (discord.DiscordException, OSError, RuntimeError, TypeError, ValueError):
            LOGGER.exception(
                "Core agent failed: invoker=%s channel=%s",
                member.id,
                message.channel.id,
            )
            await self._send_failure(message)

    async def _build_local_context(
        self,
        message: discord.Message,
        referenced: discord.Message | None,
    ) -> str:
        recent: list[discord.Message] = []
        try:
            recent = [
                item
                async for item in message.channel.history(
                    limit=LOCAL_CONTEXT_MESSAGES,
                    before=message,
                    oldest_first=False,
                )
            ]
            recent.reverse()
            if referenced is not None:
                recent = [item for item in recent if item.id != referenced.id]
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.debug(
                "Could not load local agent context: channel=%s",
                message.channel.id,
                exc_info=True,
            )

        lines = [
            "Immediate channel conversation (oldest to newest):",
            *(_render_local_message(item) for item in recent),
        ]
        if referenced is not None:
            lines.extend(
                (
                    "Message directly replied to by the asker:",
                    _render_local_message(referenced),
                )
            )
        targets = [
            target
            for target in message.mentions
            if self.bot.user is None or target.id != self.bot.user.id
        ]
        if targets:
            lines.append("Members explicitly mentioned by the asker:")
            lines.extend(
                f"- {target.display_name} (member_id={target.id}, mention={target.mention})"
                for target in targets
            )
        return "\n".join(line for line in lines if line).strip()

    async def _resolve_referenced_message(
        self,
        message: discord.Message,
    ) -> discord.Message | None:
        reference = message.reference
        if reference is None or reference.message_id is None:
            return None
        if (
            reference.channel_id is not None
            and reference.channel_id != message.channel.id
        ):
            return None
        if isinstance(reference.resolved, discord.Message):
            return reference.resolved
        try:
            return await message.channel.fetch_message(reference.message_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None

    async def _send_response(
        self,
        message: discord.Message,
        response: str,
        referenced: discord.Message | None,
    ) -> None:
        allowed_users: dict[int, discord.abc.User] = {
            member.id: member
            for member in message.mentions
            if self.bot.user is None or member.id != self.bot.user.id
        }
        if referenced is not None and not referenced.author.bot:
            allowed_users[referenced.author.id] = referenced.author
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            roles=False,
            users=list(allowed_users.values()),
            replied_user=False,
        )
        if len(response) > MAX_RESPONSE_CHARACTERS or "```" in response:
            await message.reply(
                file=discord.File(io.BytesIO(response.encode("utf-8")), filename="Elbow Helper.txt"),
                mention_author=False,
                allowed_mentions=allowed_mentions,
            )
            return
        chunks = _chunk_response(response)
        if not chunks:
            raise AgentUnavailableError("The agent returned an empty answer")
        await message.reply(
            chunks[0],
            mention_author=False,
            allowed_mentions=allowed_mentions,
        )
        for chunk in chunks[1:]:
            await message.channel.send(
                chunk,
                allowed_mentions=allowed_mentions,
            )

    @staticmethod
    async def _send_failure(message: discord.Message) -> None:
        try:
            await message.reply(
                DEFAULT_FAILURE_MESSAGE,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            LOGGER.debug("Could not send Core agent failure response", exc_info=True)


def _render_local_message(message: discord.Message) -> str:
    content = message_text(message)
    if len(content) > LOCAL_MESSAGE_CHARACTER_LIMIT:
        content = f"{content[: LOCAL_MESSAGE_CHARACTER_LIMIT - 3]}..."
    author_type = "bot" if message.author.bot else "member"
    return (
        f"- {message.author.display_name} ({author_type}, member_id={message.author.id}, "
        f"message_id={message.id}, timestamp={message.created_at.isoformat()}, "
        f"source={message.jump_url}): {content or '[no text]'}"
    )


def _chunk_response(content: str) -> list[str]:
    remaining = str(content or "").strip()
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= DISCORD_MESSAGE_LIMIT:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, DISCORD_MESSAGE_LIMIT)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, DISCORD_MESSAGE_LIMIT)
        if split_at <= 0:
            split_at = DISCORD_MESSAGE_LIMIT
        chunk = remaining[:split_at].rstrip()
        chunks.append(chunk or remaining[:DISCORD_MESSAGE_LIMIT])
        remaining = remaining[split_at:].lstrip()
    return chunks
