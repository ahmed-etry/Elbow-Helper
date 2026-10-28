"""Image bytes stay in provider content; authorized source identities survive follow-ups."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from dataclasses import replace

import discord
import httpx
from openai import BadRequestError

from elbow_helper.agent.files.images import request_images
from elbow_helper.infrastructure.ai.agent import AgentImage
from elbow_helper.infrastructure.ai.client import _DeepSeekAgentSession
from elbow_helper.agent.conversation.state import Conversation, ConversationRecord, ConversationTurn
from elbow_helper.agent.conversation.codec import encode_conversation, decode_conversation
from agent.discord_actions.management_helpers import fixture


class ImageTests(unittest.IsolatedAsyncioTestCase):
    def source(self, identifier, channel):
        return SimpleNamespace(id=identifier, channel=channel, embeds=[], content="", attachments=[
            SimpleNamespace(content_type="image/png", filename="synthetic.png", size=3,
                            read=AsyncMock(return_value=b"png"))])

    def retained(self, index, sources):
        record = ConversationRecord(request_message_id=index, member_id=10, created_at="2026-01-01",
            question="Synthetic question", generated_answer="Synthetic answer", delivered_answer="Synthetic answer",
            local_context="", evidence=(), report_ids=(), reply_ids=(1000 + index,), delivery_complete=True,
            image_sources=tuple(sources))
        return ConversationTurn("Synthetic turn", frozenset({200}), record)

    async def test_seen_images_are_refetched_newest_first_under_the_four_image_cap(self):
        context, channel, _, _ = fixture()
        context.attachment_sources = ()
        context.history = tuple(self.retained(index, ((200, index),)) for index in range(1, 7))
        sources = {index: self.source(index, channel) for index in range(1, 7)}
        channel.fetch_message = AsyncMock(side_effect=sources.__getitem__)
        images = await request_images(context)
        self.assertEqual(len(images), 4)
        self.assertEqual([call.args[0] for call in channel.fetch_message.await_args_list], [6, 5, 4, 3])
        self.assertEqual(context.state.image_sources, [(200, 6), (200, 5), (200, 4), (200, 3)])
        self.assertEqual(context.state.source_channels, {200})
        self.assertTrue(all("earlier conversation image" in image.label for image in images))

    async def test_linked_images_need_read_access_and_other_servers_are_not_fetched(self):
        context, channel, _, _ = fixture()
        context.attachment_sources = ()
        context.source_message.content = (
            "https://discord.com/channels/1/200/9 "
            "https://discord.com/channels/2/200/10 "
            "https://example.invalid/channels/1/200/11")
        channel.fetch_message = AsyncMock(return_value=self.source(9, channel))
        images = await request_images(context)
        self.assertEqual(len(images), 1)
        self.assertIn("linked message", images[0].label)
        channel.fetch_message.assert_awaited_once_with(9)
        channel.fetch_message.reset_mock()
        channel.permissions_for = lambda _: discord.Permissions(view_channel=True, read_message_history=False)
        context.state.image_sources.clear()
        self.assertEqual(await request_images(context), ())
        channel.fetch_message.assert_not_awaited()

    async def test_deleted_source_is_marked_gone_and_revoked_source_is_not_downloaded(self):
        context, channel, _, _ = fixture()
        context.attachment_sources = ()
        context.history = (self.retained(1, ((200, 9),)),)
        channel.fetch_message = AsyncMock(side_effect=discord.NotFound(
            SimpleNamespace(status=404, reason="Synthetic"), {"code": 10008, "message": "Synthetic"}))
        self.assertEqual(await request_images(context), ())
        self.assertIn("Image source is gone", context.state.image_source_status[0])
        channel.fetch_message.reset_mock()
        context.state.image_source_status.clear()
        channel.permissions_for = lambda _: discord.Permissions.none()
        self.assertEqual(await request_images(context), ())
        channel.fetch_message.assert_not_awaited()
        self.assertIn("cannot be read", context.state.image_source_status[0])

    async def test_current_and_linked_sources_are_deduplicated(self):
        context, channel, _, _ = fixture()
        source = self.source(9, channel)
        source.content = "https://discord.com/channels/1/200/9"
        context.source_message = source
        context.attachment_sources = (source,)
        context.history = (self.retained(1, ((200, 9),)),)
        channel.fetch_message = AsyncMock()
        self.assertEqual(len(await request_images(context)), 1)
        source.attachments[0].read.assert_awaited_once()
        channel.fetch_message.assert_not_awaited()

    async def test_cached_reply_is_refetched_and_a_deleted_image_is_not_reused(self):
        context, channel, _, _ = fixture()
        cached = self.source(9, channel)
        context.attachment_sources = (cached,)
        context.history = (self.retained(1, ((200, 9),)),)
        channel.fetch_message = AsyncMock(side_effect=discord.NotFound(
            SimpleNamespace(status=404, reason="Synthetic"), {"code": 10008, "message": "Synthetic"}))
        self.assertEqual(await request_images(context), ())
        channel.fetch_message.assert_awaited_once_with(9)
        cached.attachments[0].read.assert_not_awaited()
        self.assertIn("Image source is gone", context.state.image_source_status[0])

    def test_image_source_identities_survive_snapshot_roundtrip_without_image_bytes(self):
        conversation = Conversation(1, 200)
        conversation.append(self.retained(1, ((200, 9),)))
        encoded = encode_conversation(conversation, root_message_id=1, revision=0)
        restored = decode_conversation(encoded)
        self.assertEqual(restored.turns[0].record.image_sources, ((200, 9),))
        self.assertNotIn("base64", encoded.payload)
        with self.assertRaises(ValueError):
            replace(restored.turns[0].record, image_sources=((200, -1),))

    async def test_discord_and_io_failures_skip_images_without_hiding_other_errors(self):
        errors = (
            discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "Synthetic"),
            OSError("Synthetic"),
            asyncio.TimeoutError(),
        )
        attachment = SimpleNamespace(
            content_type="image/png", filename="synthetic.png", size=3, read=AsyncMock(),
        )
        proxy = SimpleNamespace(proxy_url="https://example.invalid/synthetic.png")
        source = SimpleNamespace(
            id=1, attachments=[attachment],
            embeds=[SimpleNamespace(type="image", image=proxy, thumbnail=proxy)],
        )
        download = AsyncMock()
        context = SimpleNamespace(
            source_message=source, attachment_sources=(source,),
            bot=SimpleNamespace(http=SimpleNamespace(get_from_cdn=download)),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                attachment.read.side_effect = error
                download.side_effect = error
                self.assertEqual(await request_images(context), ())
        attachment.read.side_effect = ValueError("Synthetic unexpected failure")
        with self.assertRaises(ValueError):
            await request_images(context)

    async def test_request_sources_order_and_cap(self):
        def message(identifier):
            return SimpleNamespace(id=identifier, embeds=[], attachments=[
                SimpleNamespace(
                    content_type="image/png", filename="synthetic.png", size=3,
                    read=AsyncMock(return_value=b"png"),
                )
                for _ in range(3)
            ])
        source, reply = message(1), message(2)
        context = SimpleNamespace(source_message=source, attachment_sources=(source, reply))
        images = await request_images(context)
        self.assertEqual(len(images), 4)
        self.assertIn("asker's message", images[0].label)
        self.assertIn("replied-to", images[-1].label)

    async def test_provider_recreates_text_session_once_on_first_400(self):
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="Synthetic answer", tool_calls=[]),
            finish_reason="stop",
        )], usage=None)
        error = BadRequestError(
            "Synthetic image rejection",
            response=httpx.Response(400, request=httpx.Request("POST", "https://example.invalid")),
            body=None,
        )
        create = AsyncMock(side_effect=[error, response, response])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        session = _DeepSeekAgentSession(
            client=client, system_prompt="Synthetic", prompt="Request", tools=(),
            max_output_tokens=100, images=[AgentImage("image/png", b"png", "Image 1")],
        )
        self.assertEqual((await session.advance()).content, "Synthetic answer")
        parts = create.await_args_list[0].kwargs["messages"][1]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "Request"})
        self.assertTrue(parts[2]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertIn(
            "could not be viewed", create.await_args_list[1].kwargs["messages"][1]["content"],
        )
        await session.advance()
        self.assertIsInstance(create.await_args_list[-1].kwargs["messages"][1]["content"], str)
