"""Discord reply delivery, nonce reconciliation, and transcript acknowledgement."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import sqlite3
from dataclasses import replace

import discord

from .access import require_evidence_access
from .disclosure import can_show
from .wording import ACTION_PRIVATE_ANSWER, ACTION_PREVIEW_HEADER, FAILURE_MESSAGE, LONG_REPLY_FILENAME
from .conversation.state import Conversation
from .conversation.transcripts import archive_write
from .models import AgentAttachment, AgentDelivery, AgentRequestContext
from .engine.service import AgentUnavailableError
from .actions.private_view import PrivateResultView
from .actions.answer_view import PrivateAnswerView
from .actions.preview import CONFIRMATION_TIMEOUT, ConfirmationView
from .actions.preview import preview_text
from .actions.details import prepare_preview
from .text import chunk_response as _chunk_response

LOGGER = logging.getLogger(__name__)
MAX_RESPONSE_CHARACTERS = 12_000


class AgentDeliveryUnknown(RuntimeError):
    """Discord may have accepted a send whose response was not observed."""


class AgentDeliveryMixin:
    """Agent delivery; owns no conversation or provider lifecycle."""

    async def send_response(
        self,
        message: discord.Message,
        response: str,
        referenced: discord.Message | None,
        attachments: list[AgentAttachment] | tuple[AgentAttachment, ...] = (),
        conversation: Conversation | None = None,
        *,
        delivery: AgentDelivery | None = None,
        context: AgentRequestContext | None = None,
        preview_timeout: float = CONFIRMATION_TIMEOUT,
        mention_requester: bool = False,
        _audience_override: bool = False,
        _nonce_seed: int | None = None,
        _notice_message: discord.Message | None = None,
        _notice_content: str | None = None,
        _answer_disclosure: bool = False,
    ) -> None:
        if context is not None and context.state.preview_reply is not None:
            active = delivery or AgentDelivery()
            answer_context = replace(context, state=replace(
                context.state, proposed_changes=[], preview_reply=None,
            ))
            try:
                await self.send_response(
                    message, response, referenced, attachments, conversation,
                    delivery=active, context=answer_context, mention_requester=mention_requester,
                    _answer_disclosure=True,
                )
            except BaseException:
                pending_preview = context.state.preview_reply
                if mention_requester:
                    pending_preview = f"<@{message.author.id}> {pending_preview}"
                active.generated_parts.extend(_chunk_response(pending_preview))
                active.complete = False
                raise
            active.complete = False
            preview_context = replace(context, state=replace(
                context.state, outcomes=[], attachments=[], preview_reply=None,
            ))
            await self.send_response(
                message, context.state.preview_reply, referenced, (), conversation,
                delivery=active, context=preview_context, preview_timeout=preview_timeout,
                mention_requester=mention_requester,
                _nonce_seed=_delivery_nonce(message.id, 1_000_001),
            )
            return
        reused_disclosure = False
        if (context is not None and not context.state.proposed_changes
                and (not context.state.outcomes or _answer_disclosure) and not _audience_override):
            sources = await require_evidence_access(context)
            audience_allowed = await can_show(
                message.channel, sources, context.state.required_access,
                context.guild, thread_members=context.disclosure_thread_members,
            )
            reused_disclosure = bool(
                not audience_allowed and conversation is not None
                and conversation.can_reuse_answer_disclosure(
                    context.member.id, message.channel.id,
                    context.state.source_channels, context.state.required_access,
                )
            )
            if not audience_allowed and not reused_disclosure:
                async def post(interaction):
                    async def deliver():
                        posted = AgentDelivery()
                        notice_reply_id = view.message.id
                        try:
                            await self.send_response(
                                message, response, referenced, attachments, conversation,
                                context=context, delivery=posted, _audience_override=True,
                                _nonce_seed=interaction.id, _notice_message=view.message,
                                _notice_content=notice,
                            )
                            if conversation is not None:
                                conversation.remember_answer_disclosure(
                                    context.member.id, message.channel.id,
                                    context.state.source_channels, context.state.required_access,
                                )
                        finally:
                            if conversation is not None and posted.message_ids:
                                conversation.record_answer_delivery(
                                    message.id, "\n".join(posted.text_parts),
                                    tuple(posted.message_ids), posted.complete,
                                    posted.unknown, tuple(posted.attempted_nonces),
                                    posted.uncertain_nonce,
                                    replaced_reply_id=notice_reply_id if _answer_disclosure else None,
                                    generated_parts=tuple(posted.generated_parts),
                                )
                                persistence = getattr(self, "persistence", None)
                                if persistence is not None:
                                    try:
                                        await persistence.save(self._conversations, conversation)
                                    except (OSError, sqlite3.Error, RuntimeError, TypeError, ValueError):
                                        LOGGER.exception("Agent checkpoint save failed: request=%s", message.id)
                    if conversation is None:
                        await deliver()
                    else:
                        async with conversation.lock:
                            await deliver()
                scheduled = mention_requester or getattr(message, "scheduled_run", False)
                private_view, _ = build_reply_views(message, context, None)
                view = PrivateAnswerView(context, response, attachments, post,
                                         private_view=private_view,
                                         timeout=3600.0 if scheduled else 600.0)
                notice = ACTION_PRIVATE_ANSWER
                if scheduled:
                    notice = f"<@{message.author.id}> {notice}"
                active = delivery or AgentDelivery()
                active.generated_parts.append(notice)
                sent = await self._send_delivery_part(
                    message.reply, message.channel, _delivery_nonce(message.id, 0), active,
                    notice, mention_author=False, view=view,
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False, roles=False,
                        users=[message.author] if scheduled else [], replied_user=False,
                    ),
                )
                view.message = sent
                if conversation is not None:
                    self._conversations.register_reply(conversation, sent.id)
                if delivery is not None:
                    delivery.record(sent.id, notice)
                    delivery.complete = True
                if getattr(message, "archive_reply", True):
                    await self._archive_reply(message.id, sent.id, notice)
                return
        if context is not None and context.state.proposed_changes:
            previous_preview = preview_text(context.state.proposed_changes)
            await prepare_preview(context)
            if response.startswith(previous_preview):
                response = preview_text(context.state.proposed_changes) + response[len(previous_preview):]
        has_preview = bool(context and context.state.proposed_changes)
        # A preview must not ping the people it names before the change is confirmed.
        allowed_users: dict[int, discord.abc.User] = {} if has_preview else {
            member.id: member
            for member in message.mentions
            if self.bot.user is None or member.id != self.bot.user.id
        }
        if referenced is not None and not referenced.author.bot and not has_preview:
            allowed_users[referenced.author.id] = referenced.author
        if mention_requester:
            allowed_users[message.author.id] = message.author
            response = f"<@{message.author.id}> {response}"
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            roles=False,
            users=list(allowed_users.values()),
            replied_user=False,
        )
        files = [discord.File(io.BytesIO(item.data), filename=item.filename) for item in attachments]
        if not has_preview and _needs_file(response):
            files.append(discord.File(io.BytesIO(response.encode("utf-8")), filename=LONG_REPLY_FILENAME))
            chunks = [None]
        else:
            chunks = _chunk_response(response)
        if not chunks and not files:
            raise AgentUnavailableError("The agent returned an empty answer")
        active_delivery = delivery or AgentDelivery()
        active_delivery.generated_parts.extend(
            [response] if chunks == [None] else chunks
        )
        try:
            if context is not None:
                await require_evidence_access(context)
            options = {"files": files} if files else {}
            private_view, confirm_view = build_reply_views(
                message, context, getattr(self, "action_runner", None),
                preview_timeout=preview_timeout,
            )
            await self._send_response_parts(
                message, response, chunks, options, private_view, confirm_view,
                allowed_mentions, active_delivery, delivery, context, conversation,
                nonce_seed=_nonce_seed,
                notice_message=_notice_message,
                notice_content=_notice_content,
            )
            if reused_disclosure:
                LOGGER.info(
                    "Agent answer disclosure reused: requester=%s channel=%s sources=%s levels=%s",
                    context.member.id, message.channel.id,
                    sorted(context.state.source_channels), sorted(context.state.required_access),
                )
        finally:
            for file in files:
                file.close()

    async def _send_response_parts(
        self, message, response, chunks, options, private_view, confirm_view,
        allowed_mentions, active_delivery, delivery, context, conversation,
        *, nonce_seed=None, notice_message=None, notice_content=None,
    ) -> None:
        if private_view is not None and confirm_view is None:
            options["view"] = private_view
        if confirm_view is not None and len(chunks) <= 1:
            options["view"] = confirm_view
        nonce = _delivery_nonce(nonce_seed or message.id, 0)
        if notice_message is not None and not allowed_mentions.users:
            active_delivery.attempt(nonce)
            sent = await notice_message.edit(
                content=chunks[0] if chunks else None,
                attachments=options.get("files", []), view=options.get("view"),
                allowed_mentions=allowed_mentions,
            )
        else:
            sent = await self._send_delivery_part(
                message.reply, getattr(message, "channel", None), nonce,
                active_delivery,
                chunks[0] if chunks else None,
                mention_author=False, allowed_mentions=allowed_mentions,
                **options,
            )
        if private_view is not None:
            private_view.message = sent
        if confirm_view is not None and len(chunks) <= 1:
            confirm_view.message = sent
        if confirm_view is not None and hasattr(self, "_previews"):
            self._previews[sent.id] = confirm_view
        if delivery is not None:
            delivery.record(sent.id, response if chunks == [None] else (chunks[0] if chunks else ""))
        if conversation is not None:
            self._conversations.register_reply(conversation, sent.id)
        if getattr(message, "archive_reply", True):
            await self._archive_reply(
                message.id, sent.id, response if chunks == [None] else (chunks[0] if chunks else ""),
                **({"previous_content": notice_content}
                   if notice_message is not None and not allowed_mentions.users else {}),
            )
        if notice_message is not None and allowed_mentions.users:
            await notice_message.delete()
        for index, chunk in enumerate(chunks[1:], start=1):
            if context is not None:
                await require_evidence_access(context)
            nonce = _delivery_nonce(nonce_seed or message.id, index)
            sent = await self._send_delivery_part(
                message.channel.send, message.channel, nonce,
                active_delivery, chunk, allowed_mentions=allowed_mentions,
                **({"view": confirm_view} if confirm_view is not None and index == len(chunks) - 1 else {}),
            )
            if confirm_view is not None:
                if hasattr(self, "_previews"):
                    self._previews[sent.id] = confirm_view
                if index == len(chunks) - 1:
                    confirm_view.message = sent
                    confirm_view.preview = chunk
            if delivery is not None:
                delivery.record(sent.id, chunk)
            if conversation is not None:
                self._conversations.register_reply(conversation, sent.id)
            if getattr(message, "archive_reply", True):
                await self._archive_reply(message.id, sent.id, chunk)
        if delivery is not None:
            delivery.complete = True

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
                LOGGER.exception("Agent delivery reconciliation failed: nonce=%s", nonce)
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
            part_index = len(record.attempted_nonces) - 1
            if record.generated_parts:
                part, total_parts = record.generated_parts[part_index], len(record.generated_parts)
            else:
                part, total_parts = _delivery_part(record.generated_answer, part_index)
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

    async def _archive_reply(
        self, request_id: int, reply_id: int, content: str,
        *, previous_content: str | None = None,
    ) -> None:
        if self.transcript_archive is None:
            return
        try:
            operation = (self.transcript_archive.replace_reply if previous_content is not None
                         else self.transcript_archive.record_reply)
            await archive_write(operation, message_id=reply_id,
                                request_message_id=request_id, content=content,
                                **({"previous_content": previous_content} if previous_content is not None else {}))
        except (OSError, sqlite3.Error, ValueError):
            # The reply is already visible. Do not send a misleading generation
            # failure or duplicate it because archival failed.
            LOGGER.exception("Agent transcript reply write failed: request=%s reply=%s", request_id, reply_id)

    @staticmethod
    async def _send_failure(message: discord.Message) -> None:
        try:
            await message.reply(
                FAILURE_MESSAGE,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
                nonce=_delivery_nonce(message.id, 1_000_000),
            )
        except Exception:
            LOGGER.warning(
                "Could not send agent failure response: request=%s channel=%s",
                message.id, message.channel.id, exc_info=True,
            )


def _delivery_part(response: str, part_index: int) -> tuple[str, int]:
    if type(part_index) is not int or part_index < 0:
        raise ValueError("Invalid delivery part index")
    if not response.startswith(ACTION_PREVIEW_HEADER) and _needs_file(response):
        if part_index != 0:
            raise ValueError("Delivery part is outside the generated response")
        return response, 1
    chunks = _chunk_response(response)
    if part_index >= len(chunks):
        raise ValueError("Delivery part is outside the generated response")
    return chunks[part_index], len(chunks)


def _needs_file(response: str) -> bool:
    if len(response) > MAX_RESPONSE_CHARACTERS:
        return True
    chunks = _chunk_response(response)
    open_block = False
    for chunk in chunks[:-1]:
        for _ in re.finditer(r"(?m)^\s*```", chunk):
            open_block = not open_block
        if open_block:
            return True
    return False


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


def build_reply_views(message, context, runner, *, preview_timeout=CONFIRMATION_TIMEOUT):
    private_parts = tuple(
        part for outcome in (context.state.outcomes if context else ())
        if outcome.visibility == "private" for part in outcome.private_parts
    )
    private_files = tuple(
        item for outcome in (context.state.outcomes if context else ())
        if outcome.visibility == "private" for item in outcome.attachments
    )
    private_panels = tuple(
        outcome.private_panel for outcome in (context.state.outcomes if context else ())
        if outcome.private_panel is not None
    )
    panel_labels = tuple(
        outcome.command_name for outcome in (context.state.outcomes if context else ())
        if outcome.private_panel is not None
    )
    private_view = (PrivateResultView(
        message.author.id, private_parts, private_files,
        panels=private_panels, panel_labels=panel_labels,
    ) if private_parts or private_files or private_panels else None)
    confirm_view = (ConfirmationView(message.author.id,
                                     tuple(context.state.proposed_changes), context,
                                     private_view,
                                     runner=runner, timeout=preview_timeout)
                    if context and context.state.proposed_changes else None)
    return private_view, confirm_view
