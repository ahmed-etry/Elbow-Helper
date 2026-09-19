"""Discord reply delivery, nonce reconciliation, and transcript acknowledgement."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import sqlite3

import discord

from elbow_helper.discord.interactions import DEFAULT_FAILURE_MESSAGE

from .access import require_evidence_access
from .conversation.state import Conversation
from .conversation.transcripts import archive_write
from .models import AgentAttachment, AgentDelivery, AgentRequestContext
from .service import AgentUnavailableError

LOGGER = logging.getLogger(__name__)
DISCORD_MESSAGE_LIMIT = 2_000
MAX_RESPONSE_CHARACTERS = 12_000


class AgentDeliveryUnknown(RuntimeError):
    """Discord may have accepted a send whose response was not observed."""


class AgentDeliveryMixin:
    """Delivery side of CoreAgent; owns no conversation or provider lifecycle."""

    async def _send_response(
        self,
        message: discord.Message,
        response: str,
        referenced: discord.Message | None,
        attachments: list[AgentAttachment] | tuple[AgentAttachment, ...] = (),
        conversation: Conversation | None = None,
        *,
        delivery: AgentDelivery | None = None,
        context: AgentRequestContext | None = None,
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
        files = [discord.File(io.BytesIO(item.data), filename=item.filename) for item in attachments]
        if len(response) > MAX_RESPONSE_CHARACTERS or "```" in response:
            files.append(discord.File(io.BytesIO(response.encode("utf-8")), filename="Elbow Helper.txt"))
            chunks = [None]
        else:
            chunks = _chunk_response(response)
        if not chunks and not files:
            raise AgentUnavailableError("The agent returned an empty answer")
        active_delivery = delivery or AgentDelivery()
        try:
            if context is not None:
                await require_evidence_access(context)
            options = {"files": files} if files else {}
            nonce = _delivery_nonce(message.id, 0)
            sent = await self._send_delivery_part(
                message.reply, getattr(message, "channel", None), nonce,
                active_delivery,
                chunks[0] if chunks else None,
                mention_author=False, allowed_mentions=allowed_mentions,
                **options,
            )
            if delivery is not None:
                delivery.record(sent.id, response if chunks == [None] else (chunks[0] if chunks else ""))
            if conversation is not None:
                self._conversations.register_reply(conversation, sent.id)
            await self._archive_reply(message.id, sent.id, response if chunks == [None] else (chunks[0] if chunks else ""))
            for index, chunk in enumerate(chunks[1:], start=1):
                if context is not None:
                    await require_evidence_access(context)
                nonce = _delivery_nonce(message.id, index)
                sent = await self._send_delivery_part(
                    message.channel.send, message.channel, nonce,
                    active_delivery, chunk, allowed_mentions=allowed_mentions,
                )
                if delivery is not None:
                    delivery.record(sent.id, chunk)
                if conversation is not None:
                    self._conversations.register_reply(conversation, sent.id)
                await self._archive_reply(message.id, sent.id, chunk)
            if delivery is not None:
                delivery.complete = True
        finally:
            for file in files:
                file.close()

    async def _send_delivery_part(
        self, sender, channel, nonce: int, delivery: AgentDelivery,
        content: str | None, **kwargs,
    ):
        delivery.attempt(nonce)
        try:
            return await sender(content, nonce=nonce, **kwargs)
        except asyncio.CancelledError:
            delivery.mark_unknown(nonce)
            raise
        except BaseException as error:
            if not _uncertain_delivery_error(error):
                raise
            delivery.mark_unknown(nonce)
            try:
                reconciled = await self._find_delivery_nonce(channel, nonce)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise AgentDeliveryUnknown(
                    "Discord delivery reconciliation failed"
                ) from error
            if reconciled is not None:
                delivery.reconcile(nonce)
                return reconciled
            raise AgentDeliveryUnknown(
                "Discord delivery could not be reconciled"
            ) from error

    async def _find_delivery_nonce(self, channel, nonce: int):
        history = getattr(channel, "history", None)
        bot_user = self.bot.user
        if not callable(history) or bot_user is None:
            return None
        try:
            async with asyncio.timeout(5):
                async for candidate in history(limit=25):
                    if (
                        getattr(getattr(candidate, "author", None), "id", None)
                        == bot_user.id
                        and str(getattr(candidate, "nonce", "")) == str(nonce)
                    ):
                        return candidate
        except (
            discord.DiscordException, OSError, TimeoutError, TypeError,
        ):
            return None
        return None

    async def _reconcile_unknown_deliveries(
        self, conversation: Conversation, channel,
    ) -> int:
        unknown = [
            turn.record for turn in reversed(conversation.turns)
            if turn.record is not None and turn.record.delivery_unknown
        ][:8]
        reconciled_count = 0
        for record in unknown:
            nonce = record.uncertain_nonce
            if nonce is None:
                continue
            sent = await self._find_delivery_nonce(channel, nonce)
            if sent is None:
                continue
            part, total_parts = _delivery_part(
                record.generated_answer,
                len(record.attempted_nonces) - 1,
            )
            complete = len(record.attempted_nonces) == total_parts
            conversation.reconcile_unknown_delivery(
                request_message_id=record.request_message_id,
                reply_id=sent.id, nonce=nonce, delivered_part=part,
                delivery_complete=complete,
            )
            self._conversations.register_reply(conversation, sent.id)
            await self._archive_reply(
                record.request_message_id, sent.id, part,
            )
            reconciled_count += 1
        return reconciled_count

    async def _archive_reply(self, request_id: int, reply_id: int, content: str) -> None:
        if self.transcript_archive is None:
            return
        try:
            await archive_write(self.transcript_archive.record_reply, message_id=reply_id,
                                request_message_id=request_id, content=content)
        except (OSError, sqlite3.Error, ValueError):
            # The reply is already visible. Do not send a misleading generation
            # failure or duplicate it because archival failed.
            LOGGER.exception("Agent transcript reply write failed: request=%s reply=%s", request_id, reply_id)

    @staticmethod
    async def _send_failure(message: discord.Message) -> None:
        try:
            await message.reply(
                DEFAULT_FAILURE_MESSAGE,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
                nonce=_delivery_nonce(message.id, 1_000_000),
            )
        except Exception:
            LOGGER.warning(
                "Could not send Core agent failure response: request=%s channel=%s",
                message.id, message.channel.id, exc_info=True,
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


def _delivery_part(response: str, part_index: int) -> tuple[str, int]:
    if type(part_index) is not int or part_index < 0:
        raise ValueError("Invalid delivery part index")
    if len(response) > MAX_RESPONSE_CHARACTERS or "```" in response:
        if part_index != 0:
            raise ValueError("Delivery part is outside the generated response")
        return response, 1
    chunks = _chunk_response(response)
    if part_index >= len(chunks):
        raise ValueError("Delivery part is outside the generated response")
    return chunks[part_index], len(chunks)


def _delivery_nonce(request_message_id: int, part_index: int) -> int:
    if (
        type(request_message_id) is not int or request_message_id <= 0
        or type(part_index) is not int or part_index < 0
    ):
        raise ValueError("Invalid delivery nonce identity")
    digest = hashlib.sha256(
        f"elbow-helper:{request_message_id}:{part_index}".encode("ascii")
    ).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def _uncertain_delivery_error(error: BaseException) -> bool:
    if isinstance(error, (OSError, TimeoutError)):
        return True
    status = getattr(error, "status", None)
    return (
        isinstance(error, discord.HTTPException)
        and type(status) is int and status >= 500
    )
