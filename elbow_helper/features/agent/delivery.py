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

from .access import ACCESS_VIEW_AUDIT_LOG, require_evidence_access
from .disclosure import can_show
from .wording import ACTION_PRIVATE_ANSWER, ACTION_PREVIEW_HEADER, FAILURE_MESSAGE, LONG_REPLY_FILENAME
from .conversation.state import Conversation
from .models import AgentAttachment, AgentDelivery, AgentRequestContext
from .engine.service import AgentUnavailableError
from .knowledge.answers import requires_private_knowledge_delivery
from .actions.private_view import PrivateResultView
from .actions.answer_view import PrivateAnswerView
from .actions.preview import CONFIRMATION_TIMEOUT, ConfirmationView
from .actions.preview import preview_text
from .actions.details import prepare_preview
from .actions.combined_reply import CombinedReplyView
from .files.delivery import (
    attachment_files, publish_spreadsheet, spreadsheet_links, spreadsheet_response,
)
from .files.gif_media import URLS
from .discord_actions.reply_targets import answer_reply_target
from .discord_actions.reactions import render_server_emojis
from .discord_actions.message_parts import outgoing_parts
from .discord_actions.messages import post_mentions
from .text import DISCORD_MESSAGE_LIMIT
from .text import chunk_response as _chunk_response

LOGGER = logging.getLogger(__name__)
MAX_RESPONSE_CHARACTERS = 12_000
_USER_MENTION = re.compile(r"<@!?(\d+)>")


def _log_visibility(message, context, visibility, reason, *, controls=()):
    requester = getattr(context, "member", None) or getattr(message, "author", None)
    LOGGER.info(
        "Agent answer visibility: request=%s requester=%s channel=%s visibility=%s "
        "reason=%s sources=%s levels=%s controls=%s",
        getattr(message, "id", None), getattr(requester, "id", None),
        getattr(getattr(message, "channel", None), "id", None), visibility, reason,
        sorted(context.state.source_channels), sorted(context.state.required_access), controls,
    )


class AgentDeliveryUnknown(RuntimeError):
    """Discord may have accepted a send whose response was not observed."""


async def _delivery_sources(context):
    sources = await require_evidence_access(context)
    # Generation may have cached private-thread membership before the audience
    # changed. Publication needs a fresh audience proof, including new readers.
    context.disclosure_thread_members.clear()
    return sources


class AgentDeliveryMixin:
    """Agent delivery; owns no conversation or provider lifecycle."""

    async def send_post_reply(self, message, text, referenced, context, conversation):
        requires_private_knowledge_delivery(context, text)
        delivery = context.state.reply_delivery.delivery
        answer_context = replace(context, state=replace(
            context.state, proposed_changes=[], preview_reply=None, outcomes=[],
        ))
        first = len(delivery.message_ids)
        parts = outgoing_parts(text)
        words = "\n".join(part for part, is_gif in parts if not is_gif)
        seed = message.id + 1_000_000 + len(context.state.reply_delivery.posts)
        sources = await _delivery_sources(context)
        audit_private = (ACCESS_VIEW_AUDIT_LOG in context.state.required_access
                         and not isinstance(message.channel, discord.DMChannel))
        audience_allowed = not audit_private and await can_show(
            message.channel, sources, context.state.required_access, context.guild,
            thread_members=context.disclosure_thread_members,
            request_id=message.id,
        )
        if not audience_allowed and not audit_private and conversation is not None:
            audience_allowed = conversation.can_reuse_answer_disclosure(
                context.member.id, message.channel.id,
                context.state.source_channels, context.state.required_access,
                context.state.knowledge_sources,
            )
        if not audience_allowed:
            await self._send_response(
                message, text, referenced, conversation=conversation,
                delivery=delivery, context=answer_context,
                _nonce_seed=seed, _suppress_mentions=True,
            )
        elif words:
            await self._send_response(
                message, words, referenced, conversation=conversation,
                delivery=delivery, context=answer_context,
                _nonce_seed=seed, _post_mentions=True,
            )
        for index, (part, is_gif) in enumerate(parts):
            if is_gif and audience_allowed:
                await self._send_gif(
                    message, part, context, conversation, delivery, nonce_seed=seed + index,
                )
        identifiers = delivery.message_ids[first:]
        if context.action_repository is not None:
            for identifier in identifiers:
                await asyncio.to_thread(
                    context.action_repository.record_message,
                    message_id=identifier, guild_id=context.guild.id,
                    channel_id=message.channel.id, requester_id=context.member.id,
                )
        return {"message_ids": identifiers, "channel_id": message.channel.id}

    async def send_response(self, message, response, referenced, attachments=(), conversation=None,
                            *, delivery=None, context=None, **options):
        if context is not None:
            requires_private_knowledge_delivery(context, response)
        active = delivery or AgentDelivery()
        gif = context.state.gif_url if context is not None else None
        if gif:
            response = _without_reply_gif(response, gif)
        if response or attachments or not gif:
            await self._send_response(
                message, response, referenced, attachments, conversation,
                delivery=active, context=context, **options,
            )
        if gif:
            active.complete = False
            sources = await _delivery_sources(context)
            audit_private = (ACCESS_VIEW_AUDIT_LOG in context.state.required_access
                             and not isinstance(message.channel, discord.DMChannel))
            allowed = not audit_private and (options.get("_audience_override", False)
                                            or await can_show(
                message.channel, sources, context.state.required_access, context.guild,
                thread_members=context.disclosure_thread_members,
                request_id=message.id,
            ))
            if (not allowed and not audit_private
                    and conversation is not None):
                allowed = conversation.can_reuse_answer_disclosure(
                    context.member.id, message.channel.id,
                    context.state.source_channels, context.state.required_access,
                    context.state.knowledge_sources,
                )
            if allowed:
                await self._send_gif(message, gif, context, conversation, active,
                                     nonce_seed=options.get("_nonce_seed"))
            elif not response and not attachments:
                await self._send_response(
                    message, response, referenced, attachments, conversation,
                    delivery=active, context=context, **options,
                )
            else:
                active.complete = True

    async def _send_gif(self, message, url, context, conversation, delivery, *, nonce_seed=None):
        await require_evidence_access(context)
        delivery.complete = False
        delivery.generated_parts.append(url)
        target = (
            await answer_reply_target(context, message.channel)
            if not delivery.message_ids else None
        )
        sender = message.channel.send if delivery.message_ids else (target or message).reply
        sent = await self._send_delivery_part(
            sender, message.channel, _delivery_nonce(nonce_seed or message.id, 2_000_001),
            delivery, url, allowed_mentions=discord.AllowedMentions.none(),
        )
        delivery.record(sent.id, url)
        if conversation is not None:
            self._conversations.register_reply(conversation, sent.id)
        if getattr(message, "archive_reply", True):
            await self._archive_reply(message.id, sent.id, url)
        delivery.complete = True

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
        preview_timeout: float = CONFIRMATION_TIMEOUT,
        mention_requester: bool = False,
        _audience_override: bool = False,
        _nonce_seed: int | None = None,
        _notice_message: discord.Message | None = None,
        _notice_content: str | None = None,
        _answer_disclosure: bool = False,
        _suppress_mentions: bool = False,
        _post_mentions: bool = False,
    ) -> None:
        guild = getattr(context, "guild", None) or getattr(message, "guild", None)
        knowledge_private = bool(context and requires_private_knowledge_delivery(context, response))
        response = render_server_emojis(response, guild)
        audit_private = bool(context and ACCESS_VIEW_AUDIT_LOG in context.state.required_access
                             and not isinstance(message.channel, discord.DMChannel))
        if audit_private:
            _audience_override = False
            if context.state.proposed_changes and context.state.preview_reply is None:
                context.state.preview_reply = preview_text(context.state.proposed_changes)
        if context is not None:
            attachments = await publish_spreadsheet(context, attachments)
        if context is not None and context.state.preview_reply is not None:
            await self._send_mixed_response(
                message, response, referenced, attachments, conversation, context,
                delivery or AgentDelivery(), preview_timeout, mention_requester,
            )
            return
        reused_disclosure = False
        if context is not None and _audience_override:
            _log_visibility(message, context, "public", "requester_post_override")
        if (context is not None and not context.state.proposed_changes
                and (not context.state.outcomes or context.state.has_read_answer
                     or _answer_disclosure or audit_private or knowledge_private)
                and not _audience_override):
            sources = await _delivery_sources(context)
            audience_allowed = False if audit_private else await can_show(
                message.channel, sources, context.state.required_access,
                context.guild, thread_members=context.disclosure_thread_members,
                request_id=message.id,
            )
            reused_disclosure = bool(
                not audit_private
                and not audience_allowed and conversation is not None
                and conversation.can_reuse_answer_disclosure(
                    context.member.id, message.channel.id,
                    context.state.source_channels, context.state.required_access,
                    context.state.knowledge_sources,
                )
            )
            _log_visibility(
                message, context, "public" if audience_allowed or reused_disclosure else "private",
                "audit_log_requires_private_delivery" if audit_private else
                "audience_authorized" if audience_allowed else
                "prior_disclosure_reused" if reused_disclosure else
                "private_knowledge_pass" if context.state.private_knowledge_used else
                "private_knowledge_title" if knowledge_private else
                "audience_not_proven",
                controls=() if audience_allowed or reused_disclosure else
                ("view_result",) if audit_private else ("view_result", "post_here"),
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
                                    context.state.knowledge_sources,
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
                                LOGGER.info(
                                    "Agent posted answer delivery: request=%s replies=%s complete=%s unknown=%s",
                                    message.id, posted.message_ids, posted.complete, posted.unknown,
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
                    message.reply, message.channel, _delivery_nonce(_nonce_seed or message.id, 0), active,
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
        response = spreadsheet_response(response, attachments)
        has_preview = bool(context and context.state.proposed_changes)
        # A preview must not ping the people it names before the change is confirmed.
        allowed_users: dict[int, discord.abc.User] = {} if has_preview else {
            member.id: member
            for member in message.mentions
            if self.bot.user is None or member.id != self.bot.user.id
        }
        if referenced is not None and not referenced.author.bot and not has_preview:
            allowed_users[referenced.author.id] = referenced.author
        get_member = getattr(getattr(message, "guild", None), "get_member", None)
        if not has_preview and callable(get_member):
            # The model decides who to address; it pings only members it writes as mentions.
            for member_id in dict.fromkeys(int(value) for value in _USER_MENTION.findall(response or "")):
                member = get_member(member_id)
                if member is not None and not getattr(member, "bot", False):
                    allowed_users[member.id] = member
        if mention_requester:
            allowed_users[message.author.id] = message.author
            response = f"<@{message.author.id}> {response}"
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            roles=False,
            users=list(allowed_users.values()),
            replied_user=False,
        )
        if _post_mentions:
            allowed_mentions = post_mentions(context, {}, response)
        if _suppress_mentions:
            allowed_mentions = discord.AllowedMentions.none()
        files = attachment_files(attachments)
        if not has_preview and _needs_file(response):
            files.append(discord.File(io.BytesIO(response.encode("utf-8")), filename=LONG_REPLY_FILENAME))
            chunks = [None]
        else:
            chunks = _chunk_response(response)
        if not chunks and not files and not any(item.google_link for item in attachments):
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
                attachments=attachments,
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

    async def _send_mixed_response(
        self, message, response, referenced, attachments, conversation, context,
        active, preview_timeout, mention_requester,
    ) -> None:
        response = spreadsheet_response(response, attachments)
        answer_context = replace(context, state=replace(
            context.state, proposed_changes=[], preview_reply=None,
        ))
        preview_context = replace(context, state=replace(
            context.state, outcomes=[], attachments=[], preview_reply=None, gif_url=None,
        ))
        if ACCESS_VIEW_AUDIT_LOG in context.state.required_access:
            preview_context = replace(preview_context, state=replace(
                preview_context.state,
                required_access=context.state.required_access - {ACCESS_VIEW_AUDIT_LOG},
                proposed_changes=[replace(action, preview=replace(
                    action.preview,
                    detail_access=action.preview.detail_access | {ACCESS_VIEW_AUDIT_LOG},
                )) for action in context.state.proposed_changes],
            ))
        scheduled = getattr(message, "scheduled_run", False)
        if scheduled:
            await self._send_split_response(
                message, response, referenced, attachments, conversation, answer_context,
                preview_context, active, preview_timeout, mention_requester, preview_first=False,
            )
            return
        await prepare_preview(preview_context)
        preview = preview_text(preview_context.state.proposed_changes)
        context.state.preview_reply = preview
        knowledge_private = requires_private_knowledge_delivery(answer_context, response)
        sources = await _delivery_sources(answer_context)
        audit_private = ACCESS_VIEW_AUDIT_LOG in answer_context.state.required_access
        allowed = False if audit_private else await can_show(
            message.channel, sources, answer_context.state.required_access, context.guild,
            thread_members=context.disclosure_thread_members,
            request_id=message.id,
        )
        reused_disclosure = False
        if (not allowed and conversation is not None
                and ACCESS_VIEW_AUDIT_LOG not in context.state.required_access):
            reused_disclosure = conversation.can_reuse_answer_disclosure(
                context.member.id, message.channel.id,
                context.state.source_channels, context.state.required_access,
                context.state.knowledge_sources,
            )
            allowed = reused_disclosure
        _log_visibility(
            message, answer_context, "public" if allowed else "private",
            "audit_log_requires_private_delivery" if audit_private else
            "prior_disclosure_reused" if reused_disclosure else
            "audience_authorized" if allowed else
            "private_knowledge_pass" if context.state.private_knowledge_used else
            "private_knowledge_title" if knowledge_private else
            "audience_not_proven",
            controls=("confirmation",) + (() if allowed else
                      ("view_result",) if audit_private else ("view_result", "post_here")),
        )
        answer = response if allowed else ACTION_PRIVATE_ANSWER
        private_view, _ = build_reply_views(message, answer_context, None)
        _, confirm_view = build_reply_views(
            message, preview_context, getattr(self, "action_runner", None), preview_timeout=preview_timeout,
        )

        async def changed(sent, previous, content):
            await self._archive_reply(message.id, sent.id, content, previous_content=previous)
            if conversation is not None:
                async with conversation.lock:
                    conversation.replace_reply_content(message.id, sent.id, content)
                    await self._save_reply_conversation(conversation, message.id)

        async def post(interaction):
            await require_evidence_access(answer_context)
            async with combined.lock:
                fits = (len(combined.render(answer=response)) <= DISCORD_MESSAGE_LIMIT
                        and not _answer_needs_ping(message, response, referenced, self.bot))
                if fits:
                    files = attachment_files(attachments)
                    try:
                        await combined.edit_part_locked(
                            "answer", content=response,
                            view=spreadsheet_links(private_view, attachments), attachments=files,
                        )
                    finally:
                        for file in files:
                            file.close()
            if not fits:
                posted = AgentDelivery()
                try:
                    await self.send_response(
                        message, response, referenced, attachments, conversation,
                        delivery=posted, context=answer_context, _audience_override=True,
                        _nonce_seed=interaction.id,
                    )
                finally:
                    if conversation is not None and posted.attempted_nonces:
                        async with conversation.lock:
                            conversation.record_additional_delivery(
                                message.id, "\n".join(posted.text_parts), tuple(posted.message_ids),
                                posted.complete, posted.unknown, tuple(posted.attempted_nonces),
                                posted.uncertain_nonce, tuple(posted.generated_parts),
                            )
                            await self._save_reply_conversation(conversation, message.id)
            elif answer_context.state.gif_url:
                posted = AgentDelivery()
                await self._send_gif(message, answer_context.state.gif_url, answer_context,
                                     conversation, posted, nonce_seed=interaction.id)
                if conversation is not None:
                    async with conversation.lock:
                        conversation.record_additional_delivery(
                            message.id, "\n".join(posted.text_parts), tuple(posted.message_ids),
                            posted.complete, posted.unknown, tuple(posted.attempted_nonces),
                            posted.uncertain_nonce, tuple(posted.generated_parts),
                        )
            if conversation is not None:
                async with conversation.lock:
                    conversation.remember_answer_disclosure(
                        context.member.id, message.channel.id,
                        context.state.source_channels, context.state.required_access,
                        context.state.knowledge_sources,
                    )
                    await self._save_reply_conversation(conversation, message.id)

        answer_view = (
            spreadsheet_links(private_view, attachments) if allowed else PrivateAnswerView(
                answer_context, response, attachments, post, private_view=private_view,
            )
        )
        combined = CombinedReplyView(
            preview, answer, confirm_view, answer_view,
            preview_first=context.state.preview_first, on_change=changed,
        )
        if (len(combined.render()) > DISCORD_MESSAGE_LIMIT or mention_requester
                or _answer_needs_ping(message, response, referenced, self.bot)
                or allowed and _needs_file(response)):
            combined.stop()
            await self._send_split_response(
                message, response, referenced, attachments, conversation, answer_context,
                preview_context, active, preview_timeout, mention_requester,
                preview_first=context.state.preview_first,
            )
            return
        files = attachment_files(attachments if allowed else ())
        content = combined.render()
        active.generated_parts.append(content)
        try:
            await require_evidence_access(context)
            sent = await self._send_delivery_part(
                message.reply, message.channel, _delivery_nonce(message.id, 0), active,
                content, mention_author=False, allowed_mentions=discord.AllowedMentions.none(),
                view=combined, **({"files": files} if files else {}),
            )
        except BaseException:
            combined.stop()
            raise
        finally:
            for file in files:
                file.close()
        combined.start(sent)
        if hasattr(self, "_previews"):
            self._previews[sent.id] = confirm_view
        active.record(sent.id, content)
        active.complete = True
        if conversation is not None:
            self._conversations.register_reply(conversation, sent.id)
        if getattr(message, "archive_reply", True):
            await self._archive_reply(message.id, sent.id, content)

    async def _send_split_response(
        self, message, response, referenced, attachments, conversation, answer_context,
        preview_context, active, preview_timeout, mention_requester, *, preview_first,
    ) -> None:
        preview = preview_text(preview_context.state.proposed_changes)
        parts = [(preview, preview_context, (), False), (response, answer_context, attachments, True)]
        if not preview_first:
            parts.reverse()
        for index, (content, selected, files, answer) in enumerate(parts):
            active.complete = False
            try:
                await self._send_response(
                    message, content, referenced, files, conversation, delivery=active, context=selected,
                    preview_timeout=preview_timeout, mention_requester=mention_requester,
                    _answer_disclosure=answer,
                    _nonce_seed=None if index == 0 else _delivery_nonce(message.id, 1_000_001),
                )
            except BaseException:
                if index == 0:
                    pending = parts[1][0]
                    if mention_requester:
                        pending = f"<@{message.author.id}> {pending}"
                    active.generated_parts.extend(_chunk_response(pending))
                active.complete = False
                raise

    async def _save_reply_conversation(self, conversation, request_id):
        persistence = getattr(self, "persistence", None)
        if persistence is not None:
            try:
                await persistence.save(self._conversations, conversation)
            except (OSError, sqlite3.Error, RuntimeError, TypeError, ValueError):
                LOGGER.exception("Agent checkpoint save failed: request=%s", request_id)

    async def _send_response_parts(
        self, message, response, chunks, options, private_view, confirm_view,
        allowed_mentions, active_delivery, delivery, context, conversation,
        *, nonce_seed=None, notice_message=None, notice_content=None, attachments=(),
    ) -> None:
        if private_view is not None and confirm_view is None:
            options["view"] = private_view
        if confirm_view is not None and len(chunks) <= 1:
            options["view"] = confirm_view
        view = spreadsheet_links(options.get("view"), attachments)
        if view is not None:
            options["view"] = view
        target = (
            await answer_reply_target(context, getattr(message, "channel", None))
            if confirm_view is None else None
        )
        ping_author = bool(target is not None and context.state.ping_author)
        if target is not None:
            allowed_mentions = allowed_mentions.merge(discord.AllowedMentions(
                replied_user=ping_author,
            ))
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
                (target or message).reply, getattr(message, "channel", None), nonce,
                active_delivery,
                chunks[0] if chunks else None,
                mention_author=ping_author, allowed_mentions=allowed_mentions,
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
        if target is not None:
            allowed_mentions = allowed_mentions.merge(discord.AllowedMentions(replied_user=False))
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
            self.transcript_archive.enqueue_write(operation, message_id=reply_id,
                request_message_id=request_id, content=content,
                **({"previous_content": previous_content} if previous_content is not None else {}))
        except Exception:
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


def _without_reply_gif(text: str, url: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\(" + re.escape(url) + r"\)", r"\1", text)
    text = text.replace("<" + url + ">", "")

    def remove(match):
        value = match.group(0)
        return value[len(url):] if value.rstrip(").,>") == url else value

    return URLS.sub(remove, text).strip()


def _answer_needs_ping(message, response, referenced, bot) -> bool:
    named = {int(value) for value in _USER_MENTION.findall(response or "")}
    candidates = {member.id for member in getattr(message, "mentions", ())
                  if bot.user is None or member.id != bot.user.id}
    if referenced is not None and not referenced.author.bot:
        candidates.add(referenced.author.id)
    get_member = getattr(getattr(message, "guild", None), "get_member", None)
    if callable(get_member):
        candidates.update(member_id for member_id in named
                          if (member := get_member(member_id)) is not None and not getattr(member, "bot", False))
    return bool(named & candidates)


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
    if private_view is not None:
        _log_visibility(message, context, "private", "feature_private_result", controls=("view_result",))
    confirm_view = (ConfirmationView(message.author.id,
                                     tuple(context.state.proposed_changes), context,
                                     private_view,
                                     runner=runner, timeout=preview_timeout)
                    if context and context.state.proposed_changes else None)
    return private_view, confirm_view
