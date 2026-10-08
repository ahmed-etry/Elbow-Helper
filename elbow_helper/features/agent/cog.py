"""Mention-driven Discord surface for the agent."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import logging
import re
import sqlite3
import time
from weakref import WeakValueDictionary

import discord
from discord.ext import commands

from elbow_helper.configuration.guild import GUILD_ID

from .conversation.preparation import ConversationContextMixin
from .conversation.turns import AgentTurnMixin
from .delivery import AgentDeliveryMixin, AgentDeliveryUnknown
from .access import AgentAccessLost, require_evidence_access
from .access import has_agent_entry_access
from .access import require_access
from .conversation.state import Conversation, ConversationStore
from .text import message_text, render_member_mentions
from .models import AgentDelivery, AgentRequestContext, AgentIdentity
from .identity import agent_identity, member_identity
from .engine.service import AgentUnavailableError
from .engine.service import AgentService
from .conversation.transcripts import TranscriptArchive, archive_write
from .conversation.persistence import ConversationPersistence


LOGGER = logging.getLogger(__name__)
LOCAL_CONTEXT_MESSAGES = 8
LOCAL_MESSAGE_CHARACTER_LIMIT = 1_200
AGENT_REQUEST_TIMEOUT_SECONDS = 480.0
AGENT_QUEUE_WAIT_TIMEOUT_SECONDS = 120.0
AGENT_CONCURRENCY = 4
MAX_PENDING_REQUESTS = 64


class AgentCog(AgentTurnMixin, ConversationContextMixin, AgentDeliveryMixin, commands.Cog):
    """Answer eligible mentions and run confirmed member requests."""

    def __init__(
        self,
        bot: commands.Bot,
        *,
        account_links,
        clan_health,
        message_search,
        thread_discovery=None,
        roster_queries,
        cwl_queries=None,
        war_queries=None,
        transfer_queries=None,
        hibernation_queries=None,
        support_queries=None,
        recruitment_queries=None,
        examination_queries=None,
        record_queries=None,
        achievement_queries=None,
        event_queries=None,
        member_lifecycle_queries=None,
        clan_reporting_queries=None,
        role_connection_queries=None,
        knowledge_store=None,
        data_guide=None,
        research_jobs=None,
        research_runner=None,
        action_runner=None,
        action_repository=None,
        transcript_archive: TranscriptArchive | None = None,
        persistence: ConversationPersistence | None = None,
    ):
        self.bot = bot
        self.account_links = account_links
        self.clan_health = clan_health
        self.message_search = message_search
        self.thread_discovery = thread_discovery
        self.roster_queries = roster_queries
        self.cwl_queries = cwl_queries
        self.war_queries = war_queries
        self.transfer_queries = transfer_queries
        self.hibernation_queries = hibernation_queries
        self.support_queries = support_queries
        self.recruitment_queries = recruitment_queries
        self.examination_queries = examination_queries
        self.record_queries = record_queries
        self.achievement_queries = achievement_queries
        self.event_queries = event_queries
        self.member_lifecycle_queries = member_lifecycle_queries
        self.clan_reporting_queries = clan_reporting_queries
        self.role_connection_queries = role_connection_queries
        self.knowledge_store = knowledge_store
        self.research_jobs = research_jobs
        self.research_runner = research_runner
        self.action_runner = action_runner
        self.scheduled_runner = None
        if self.action_runner is not None:
            self.action_runner.on_finish = self._record_action_outcome
        self.action_repository = action_repository
        self.transcript_archive = transcript_archive
        self.persistence = persistence
        self._cleanup_task: asyncio.Task | None = None
        self.service = AgentService(bot.agent_model, data_guide=data_guide)
        self._conversations = ConversationStore()
        self._member_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        self._previews: WeakValueDictionary[int, object] = WeakValueDictionary()
        self._tasks: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(AGENT_CONCURRENCY)
        self._application_owner: AgentIdentity | None = None
        self._application_owner_loaded = False

    @property
    def application_owner(self) -> AgentIdentity | None:
        return self._application_owner

    async def cog_load(self) -> None:
        await self._load_application_owner()
        if self.persistence is not None:
            await self.persistence.restore(self._conversations, guild_id=GUILD_ID)
        if (
            self.persistence is not None or self.research_jobs is not None
        ):
            self._cleanup_task = asyncio.create_task(self._prune_checkpoints())
        if self.research_runner is not None:
            self.research_runner.start()
        if self.action_runner is not None:
            self.action_runner.start()
        if self.scheduled_runner is not None:
            self.scheduled_runner.start()

    async def _load_application_owner(self) -> None:
        if self._application_owner_loaded:
            return
        self._application_owner_loaded = True
        application_info = getattr(self.bot, "application_info", None)
        if not callable(application_info):
            return
        try:
            async with asyncio.timeout(10):
                info = await application_info()
            team = getattr(info, "team", None)
            owner = getattr(team, "owner", None) if team is not None else getattr(info, "owner", None)
            self._application_owner = member_identity(owner)
        except (discord.DiscordException, OSError, TimeoutError):
            LOGGER.warning("Agent application owner identity is unavailable", exc_info=True)

    async def _prune_checkpoints(self) -> None:
        while True:
            await asyncio.sleep(60)
            if self.persistence is not None:
                try:
                    await self.persistence.prune(self._conversations)
                except (OSError, sqlite3.Error, RuntimeError):
                    LOGGER.exception("Agent checkpoint expiry failed")
            if self.research_jobs is not None:
                try:
                    await asyncio.to_thread(self.research_jobs.prune)
                except (OSError, sqlite3.Error, RuntimeError):
                    LOGGER.exception("Agent research job expiry failed")
            if self.action_repository is not None:
                try:
                    await asyncio.to_thread(self.action_repository.prune_log)
                except (OSError, sqlite3.Error, RuntimeError):
                    LOGGER.exception("Agent action log expiry failed")

    async def _run_member_request(self, message, member, question, conversation,
                                  queued_at):
        member_lock = self._member_locks.setdefault(member.id, asyncio.Lock())
        async with AsyncExitStack() as locks:
            async with asyncio.timeout(AGENT_QUEUE_WAIT_TIMEOUT_SECONDS):
                await locks.enter_async_context(conversation.lock)
                await locks.enter_async_context(member_lock)
                await locks.enter_async_context(self._semaphore)
            request_started_at = time.monotonic()
            async with asyncio.timeout(AGENT_REQUEST_TIMEOUT_SECONDS):
                LOGGER.info(
                    "Agent queue: request=%s invoker=%s wait_ms=%s",
                    message.id, member.id,
                    int((time.monotonic() - queued_at) * 1_000),
                )
                require_access(message.guild, member.id, message.channel)
                reconciled = await self._reconcile_unknown_deliveries(
                    conversation, message.channel,
                )
                if reconciled and self.persistence is not None:
                    try:
                        await self.persistence.save(
                            self._conversations, conversation,
                        )
                    except (
                        OSError, sqlite3.Error, RuntimeError,
                        TypeError, ValueError,
                    ):
                        LOGGER.exception(
                            "Agent delivery reconciliation save failed: request=%s",
                            message.id,
                        )
                if conversation.record_for_request(message.id) is not None:
                    LOGGER.debug("Ignoring already delivered agent request: message=%s", message.id)
                    return
                await self._answer(
                    message, member, question, conversation,
                    deadline_monotonic=request_started_at + AGENT_REQUEST_TIMEOUT_SECONDS,
                )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self._is_agent_request(message):
            if not await self._restore_persisted_reply(message):
                return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        if not has_agent_entry_access(member):
            return
        reference = getattr(message, "reference", None)
        preview = self._previews.get(getattr(reference, "message_id", None))
        if preview is not None and preview.owner_id == member.id:
            await preview.invalidate()
        question = self._extract_question(message)
        if not question:
            return
        if len(self._tasks) >= MAX_PENDING_REQUESTS:
            await self._send_failure(message)
            return
        task = asyncio.current_task()
        self._tasks.add(task)
        conversation = None
        queued_at = time.monotonic()
        try:
            conversation = self._reply_conversation(message) or self._conversations.create(
                message.guild.id, message.channel.id, message.id,
            )
            conversation.pending += 1
            await self._run_member_request(
                message, member, question, conversation, queued_at,
            )
        except AgentAccessLost as error:
            LOGGER.warning(
                "Agent access lost: request=%s invoker=%s channel=%s error=%s",
                message.id, member.id, message.channel.id, error,
            )
            try:
                require_access(message.guild, member.id, message.channel)
            except AgentAccessLost:
                pass
            else:
                await self._send_failure(message)
        except AgentDeliveryUnknown:
            LOGGER.warning(
                "Agent delivery outcome unknown: request=%s invoker=%s",
                message.id, member.id,
            )
        except TimeoutError:
            LOGGER.warning(
                "Agent request timed out: request=%s invoker=%s channel=%s elapsed_ms=%s",
                message.id, member.id, message.channel.id,
                int((time.monotonic() - queued_at) * 1_000),
            )
            await self._send_failure(message)
        except asyncio.CancelledError:
            LOGGER.warning(
                "Agent request cancelled: request=%s invoker=%s channel=%s elapsed_ms=%s",
                message.id, member.id, message.channel.id,
                int((time.monotonic() - queued_at) * 1_000),
            )
            raise
        except Exception:
            # This is the Discord event boundary: unexpected failures need the
            # same reply as known failures, without restarting paid generation.
            LOGGER.exception(
                "Agent request failed: request=%s invoker=%s channel=%s",
                message.id, member.id, message.channel.id,
            )
            await self._send_failure(message)
        finally:
            if conversation is not None:
                conversation.pending -= 1
            self._tasks.discard(task)

    def cog_unload(self):
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
        if self.research_runner is not None:
            self.research_runner.cancel()
        if self.action_runner is not None:
            self.action_runner.cancel()
        if self.scheduled_runner is not None:
            self.scheduled_runner.cancel()
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
            and (bot_user.id in message.raw_mentions or self._reply_conversation(message) is not None)
        )

    def _reply_conversation(self, message: discord.Message) -> Conversation | None:
        reference = getattr(message, "reference", None)
        if reference is None or reference.channel_id not in (None, message.channel.id):
            return None
        return self._conversations.find(message.guild.id, message.channel.id, reference.message_id)

    async def _restore_persisted_reply(self, message: discord.Message) -> bool:
        if self.persistence is None or message.guild is None:
            return False
        bot_user = self.bot.user
        reference = getattr(message, "reference", None)
        if (
            bot_user is None
            or message.guild.id != GUILD_ID
            or getattr(message.author, "bot", True)
            or getattr(message, "webhook_id", None)
            or reference is None
            or reference.message_id is None
            or reference.channel_id not in (None, message.channel.id)
            or not has_agent_entry_access(message.author)
        ):
            return False
        try:
            conversation = await self.persistence.restore_reply(
                self._conversations, guild_id=message.guild.id,
                channel_id=message.channel.id, reply_id=reference.message_id,
            )
        except (OSError, sqlite3.Error, RuntimeError, TypeError, ValueError):
            LOGGER.exception(
                "Agent reply checkpoint lookup failed: reply=%s",
                reference.message_id,
            )
            return False
        return conversation is not None

    def _extract_question(self, message: discord.Message) -> str:
        bot_user = self.bot.user
        if bot_user is None:
            return ""
        mention_pattern = re.compile(rf"<@!?{bot_user.id}>")
        content = message.content or ""
        if not mention_pattern.sub("", content).strip():
            return ""
        identity = agent_identity(self.bot, getattr(message, "guild", None))
        if identity is None:
            return content.strip()
        return mention_pattern.sub(lambda _: f"@{identity.display_name}", content).strip()


    async def _answer(
        self,
        message: discord.Message,
        member: discord.Member,
        question: str,
        conversation: Conversation,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        member = require_access(message.guild, member.id, message.channel)
        referenced = await self._resolve_referenced_message(message)
        context, root_id = self._request_context(
            message, member, referenced, conversation, deadline_monotonic,
        )
        await self._load_authorized_reports(conversation, context)
        await self._check_sources(context)
        if self.transcript_archive is not None:
            await archive_write(
                self.transcript_archive.record_request, message_id=message.id,
                guild_id=message.guild.id, channel_id=message.channel.id,
                root_message_id=root_id, member_id=member.id, created_at=message.created_at.isoformat(),
                content=message.content,
                replied_to_message_id=getattr(message.reference, "message_id", None),
            )
        local_context = await self._build_local_context(message, referenced, conversation)
        history = await self._conversation_history(conversation, context)

        try:
            async with message.channel.typing():
                response = await self.service.answer(
                    question=question,
                    local_context=local_context,
                    context=context,
                    conversation_history=history,
                )
            require_access(message.guild, member.id, message.channel)
            await self._check_sources(context)
            delivery = AgentDelivery()
            try:
                await self.send_response(
                    message, response, referenced, context.state.attachments, conversation,
                    delivery=delivery, context=context,
                )
            finally:
                await self._record_turn(
                    message, member, question, response, local_context,
                    context, delivery, conversation,
                )
        except AgentAccessLost:
            raise
        except AgentUnavailableError as error:
            LOGGER.warning(
                "Agent unavailable: request=%s invoker=%s channel=%s error=%s",
                message.id,
                member.id,
                message.channel.id,
                error,
            )
            await self._send_failure(message)

    @staticmethod
    async def _check_sources(context: AgentRequestContext) -> None:
        await require_evidence_access(context)

    async def _build_local_context(
        self,
        message: discord.Message,
        referenced: discord.Message | None,
        conversation: Conversation | None = None,
    ) -> str:
        recent: list[discord.Message] = []
        try:
            anchor = referenced if referenced is not None else message
            recent = [
                item
                async for item in message.channel.history(
                    limit=LOCAL_CONTEXT_MESSAGES,
                    before=anchor,
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

        identity = agent_identity(self.bot, getattr(message, "guild", None))

        def render(item: discord.Message) -> str:
            guild = getattr(message, "guild", None)
            owner = (self._conversations.find_message(guild.id, message.channel.id, item.id)
                     if guild is not None else None)
            bot_id = getattr(getattr(self.bot, "user", None), "id", None)
            is_request = bot_id is not None and bot_id in getattr(item, "raw_mentions", ())
            is_agent_reply = bot_id is not None and item.author.id == bot_id
            other = ((owner is not None and owner is not conversation)
                     or (owner is None and (is_request or is_agent_reply)))
            if not other:
                return _render_local_message(item, identity=identity)
            now = getattr(message, "created_at", None) or datetime.now(timezone.utc)
            age = max(0, int((now - item.created_at).total_seconds()))
            return _render_local_message(item, identity=identity,
                                         context_note=f"other agent conversation, {age}s old")

        lines = [
            "Immediate channel conversation (oldest to newest):",
            *(render(item) for item in recent),
        ]
        if referenced is not None:
            lines.extend(
                (
                    "Message directly replied to by the asker:",
                    render(referenced),
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
            resolved = reference.resolved
            if (
                getattr(getattr(resolved, "guild", None), "id", None) == message.guild.id
                and getattr(getattr(resolved, "channel", None), "id", None) == message.channel.id
            ):
                return resolved
            return None
        try:
            return await message.channel.fetch_message(reference.message_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None


def _render_local_message(
    message: discord.Message, *, identity: AgentIdentity | None = None, context_note: str = "",
) -> str:
    content = render_member_mentions(message_text(message), message, identity=identity)
    if len(content) > LOCAL_MESSAGE_CHARACTER_LIMIT:
        content = f"{content[: LOCAL_MESSAGE_CHARACTER_LIMIT - 3]}..."
    author_type = ("you" if identity is not None and message.author.id == identity.member_id
                   else "bot" if message.author.bot else "member")
    return (
        f"- {message.author.display_name} ({author_type}, member_id={message.author.id}, "
        f"message_id={message.id}, timestamp={message.created_at.isoformat()}, "
        f"source={message.jump_url}"
        f"{', ' + context_note if context_note else ''}): {content or '[no text]'}"
    )
