"""Visible media suppresses prose; only a confirmed rejection permits fallback."""
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.agent.delivery import AgentDeliveryMixin, AgentDeliveryUnknown
from elbow_helper.agent.models import AgentAttachment, AgentDelivery
from agent.engine.test_agent_service import _context


class ImageOnlyDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = _context()
        self.message = self.context.source_message
        self.message.id, self.message.mentions = 300, []
        self.message.author = self.context.member
        self.message.archive_reply = False
        self.message.reply = AsyncMock(return_value=SimpleNamespace(id=701))
        self.message.channel.send = AsyncMock(return_value=SimpleNamespace(id=702))
        self.surface = AgentDeliveryMixin()
        self.surface.bot = SimpleNamespace(user=self.context.guild.me)
        self.delivery = AgentDelivery()
        guards = ExitStack()
        for name in ('require_evidence_access', 'assert_answer_scope'):
            guards.enter_context(patch('elbow_helper.agent.delivery.' + name, AsyncMock()))
        self.addCleanup(guards.close)

    async def send(self, text, attachments=()):
        await self.surface.send_response(self.message, text, None, attachments,
            context=self.context, delivery=self.delivery)

    async def test_each_supported_image_attachment_has_no_caption(self):
        for extension in ('png', 'jpg', 'jpeg', 'webp', 'gif'):
            with self.subTest(extension=extension):
                self.message.reply.reset_mock()
                await self.send('Synthetic fallback.', [AgentAttachment('sample.' + extension, b'media')])
                self.assertIsNone(self.message.reply.await_args.args[0])
                self.assertEqual(len(self.message.reply.await_args.kwargs['files']), 1)
                self.message.channel.send.assert_not_awaited()

    async def test_non_image_file_keeps_its_requested_explanation(self):
        await self.send('Synthetic explanation.', [AgentAttachment('sample.csv', b'column\nvalue')])
        self.assertEqual(self.message.reply.await_args.args, ('Synthetic explanation.',))

    async def test_selected_image_sends_no_prose_even_if_caller_supplies_it(self):
        self.context.state.gif_url = 'https://tenor.com/view/Synthetic'
        await self.send('Synthetic fallback.')
        self.message.reply.assert_awaited_once()
        self.assertEqual(self.message.reply.await_args.args, (self.context.state.gif_url,))
        self.assertEqual(self.delivery.text_parts, [self.context.state.gif_url])
        self.assertEqual(self.context.state.gif_fallback_text, 'Synthetic fallback.')

    async def test_definite_media_rejection_sends_only_the_authored_fallback(self):
        self.context.state.gif_url = 'https://tenor.com/view/Synthetic'
        rejected = discord.HTTPException(SimpleNamespace(status=400, reason='Synthetic rejection'), 'invalid file')
        self.message.reply.side_effect = [rejected, SimpleNamespace(id=701)]
        await self.send('Synthetic fallback.')
        self.assertIsNone(self.context.state.gif_url)
        self.assertEqual(self.delivery.text_parts, ['Synthetic fallback.'])
        self.assertTrue(self.delivery.complete)
        self.assertFalse(self.delivery.unknown)

    async def test_uncertain_media_send_never_sends_a_caption_or_fallback(self):
        self.context.state.gif_url = 'https://tenor.com/view/Synthetic'
        self.message.reply.side_effect = OSError('Synthetic disconnect')
        self.surface._find_delivery_nonce = AsyncMock(return_value=None)
        with self.assertRaises(AgentDeliveryUnknown):
            await self.send('Synthetic fallback.')
        self.message.reply.assert_awaited_once()
        self.assertTrue(self.delivery.unknown)
        self.assertEqual(self.delivery.text_parts, [])

    async def test_raw_provider_link_cannot_smuggle_a_caption(self):
        await self.send('Synthetic caption.\nhttps://tenor.com/view/Synthetic')
        self.assertEqual(self.message.reply.await_args.args, ('https://tenor.com/view/Synthetic',))

    async def test_direct_image_link_cannot_smuggle_a_caption(self):
        await self.send('Synthetic caption.\nhttps://example.invalid/image.PNG?version=2')
        self.assertEqual(self.message.reply.await_args.args,
            ('https://example.invalid/image.PNG?version=2',))

    async def test_requester_mention_cannot_become_an_image_caption(self):
        await self.surface.send_response(self.message, 'Synthetic fallback.', None,
            [AgentAttachment('sample.png', b'media')], context=self.context,
            delivery=self.delivery, mention_requester=True)
        self.assertIsNone(self.message.reply.await_args.args[0])
