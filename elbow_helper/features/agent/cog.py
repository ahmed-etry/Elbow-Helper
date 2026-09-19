"""Mention-driven Discord surface for the Core agent beta."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from weakref import WeakValueDictionary

import discord
from discord.ext import commands

from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import CORE

from .conversation.preparation import ConversationContextMixin
from .delivery import AgentDeliveryMixin, AgentDeliveryUnknown
from .access import AgentAccessLost, require_access, require_evidence_access
from .conversation.state import (
    Conversation, ConversationRecord, ConversationStore, ConversationTurn,
)
from .message_content import message_text
from .models import AgentDelivery, AgentRequestContext, AgentTurnState
from .reports.knowledge import KnowledgeReport
from .service import AgentUnavailableError, CoreAgentService
from .conversation.transcripts import TranscriptArchive, archive_write
from .conversation.persistence import ConversationPersistence


LOGGER = logging.getLogger(__name__)
LOCAL_CONTEXT_MESSAGES = 8
LOCAL_MESSAGE_CHARACTER_LIMIT = 1_200
AGENT_REQUEST_TIMEOUT_SECONDS = 480.0
AGENT_CONCURRENCY = 4
MAX_PENDING_REQUESTS = 64


class CoreAgent(ConversationContextMixin, AgentDeliveryMixin, commands.Cog):
    """Answer direct mentions from Core members without changing server state."""

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
        research_jobs=None,
        research_runner=None,
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
        self.transcript_archive = transcript_archive
        self.persistence = persistence
        self._cleanup_task: asyncio.Task | None = None
        self.service = CoreAgentService(bot.agent_model)
        self._conversations = ConversationStore()
        self._member_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        self._tasks: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(AGENT_CONCURRENCY)

    async def cog_load(self) -> None:
        if self.persistence is not None:
            await self.persistence.restore(self._conversations, guild_id=GUILD_ID)
        if (
            self.persistence is not None or self.research_jobs is not None
        ):
            self._cleanup_task = asyncio.create_task(self._prune_checkpoints())
        if self.research_runner is not None:
            self.research_runner.start()

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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self._is_agent_request(message):
            if not await self._restore_persisted_reply(message):
                return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        if not any(role.id in CORE for role in member.roles):
            return
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
            member_lock = self._member_locks.setdefault(member.id, asyncio.Lock())
            async with asyncio.timeout(AGENT_REQUEST_TIMEOUT_SECONDS):
                async with conversation.lock, member_lock, self._semaphore:
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
                    await self._answer(message, member, question, conversation)
        except AgentAccessLost:
            LOGGER.info("Agent access lost: invoker=%s", member.id)
        except AgentDeliveryUnknown:
            LOGGER.warning(
                "Agent delivery outcome unknown: request=%s invoker=%s",
                message.id, member.id,
            )
        except TimeoutError:
            await self._send_failure(message)
        except (discord.DiscordException, OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
            LOGGER.exception("Agent request failed: invoker=%s", member.id)
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
            or not any(role.id in CORE for role in getattr(message.author, "roles", ()))
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
        return mention_pattern.sub("", message.content or "").strip()

    async def _answer(
        self,
        message: discord.Message,
        member: discord.Member,
        question: str,
        conversation: Conversation,
    ) -> None:
        member = require_access(message.guild, member.id, message.channel)
        referenced = await self._resolve_referenced_message(message)
        root_id = next(
            key for key, value in self._conversations.entries()
            if value is conversation
        )
        context = AgentRequestContext(
            bot=self.bot,
            guild=message.guild,
            member=member,
            source_message=message,
            account_links=self.account_links,
            clan_health=self.clan_health,
            message_search=self.message_search,
            thread_discovery=self.thread_discovery,
            state=AgentTurnState(
                source_channels={message.channel.id}, working=conversation.working,
            ),
            history=tuple(conversation.turns),
            roster_queries=self.roster_queries,
            cwl_queries=self.cwl_queries,
            war_queries=self.war_queries,
            transfer_queries=self.transfer_queries,
            hibernation_queries=self.hibernation_queries,
            support_queries=self.support_queries,
            recruitment_queries=self.recruitment_queries,
            examination_queries=self.examination_queries,
            record_queries=self.record_queries,
            achievement_queries=self.achievement_queries,
            event_queries=self.event_queries,
            member_lifecycle_queries=self.member_lifecycle_queries,
            clan_reporting_queries=self.clan_reporting_queries,
            role_connection_queries=self.role_connection_queries,
            knowledge_store=self.knowledge_store,
            research_jobs=self.research_jobs,
            conversation_root_id=root_id,
            attachment_sources=tuple(item for item in (message, referenced) if item is not None),
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
        local_context = await self._build_local_context(message, referenced)
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
                await self._send_response(
                    message, response, referenced, context.state.attachments, conversation,
                    delivery=delivery, context=context,
                )
            finally:
                if delivery.attempted_nonces:
                    self._commit_reports(conversation, context.state)
                    conversation.working = context.state.working
                    delivered_answer = response if delivery.complete else "\n".join(delivery.text_parts)
                    conversation.append(ConversationTurn(
                        text=json.dumps({
                            "asker": member.display_name, "member_id": member.id,
                            "question": question, "answer": delivered_answer[:16_000],
                            "answer_truncated": len(delivered_answer) > 16_000,
                            "local_context": local_context[:12_000],
                            "lookup_excerpts": [item[:5_000] for item in context.state.evidence[-4:]],
                            "report_ids": list(context.state.reports),
                        }, ensure_ascii=False),
                        source_channels=frozenset(context.state.source_channels),
                        required_access=frozenset(context.state.required_access),
                        knowledge_refs=tuple(sorted(
                            (section.section_id, section.content_sha256)
                            for report in context.state.reports.values()
                            if isinstance(report, KnowledgeReport)
                            for section in report.sections
                        )),
                        record=ConversationRecord(
                            request_message_id=message.id,
                            member_id=member.id,
                            created_at=message.created_at.isoformat(),
                            question=question,
                            generated_answer=response,
                            delivered_answer=delivered_answer,
                            local_context=local_context,
                            evidence=tuple(context.state.evidence),
                            report_ids=tuple(context.state.reports),
                            reply_ids=tuple(delivery.message_ids),
                            delivery_complete=delivery.complete,
                            delivery_unknown=delivery.unknown,
                            attempted_nonces=tuple(delivery.attempted_nonces),
                            uncertain_nonce=delivery.uncertain_nonce,
                        ),
                    ))
                    self._refresh_history_checkpoint(
                        conversation, context.state,
                        created_at=message.created_at,
                        previous_turn_count=len(conversation.turns) - 1,
                    )
                    if self.persistence is not None:
                        try:
                            await self.persistence.save(self._conversations, conversation)
                        except (OSError, sqlite3.Error, RuntimeError, TypeError, ValueError):
                            LOGGER.exception("Agent checkpoint save failed: request=%s", message.id)
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

    @staticmethod
    async def _check_sources(context: AgentRequestContext) -> None:
        await require_evidence_access(context)

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
