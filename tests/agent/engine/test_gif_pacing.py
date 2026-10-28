"""Graded image delivery has no random gate or required-image retry."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.delivery import AgentDeliveryMixin
from elbow_helper.agent.discord_actions.gifs import channel_gif_history
from elbow_helper.agent.discord_actions.gif_library import ITEM_BY_ID
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.models import AgentDelivery
from agent.engine.helpers import social_step
from agent.engine.test_agent_service import _AgentModel, _AgentSession, _context


class GifMatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_grade_controls_exact_item_alone_or_fallback_with_one_round(self):
        url = 'https://tenor.com/view/synthetic-123'
        item = {'id': 'synthetic', 'media': url, 'use': 'Synthetic celebration', 'shows': 'Celebration', 'group': 'win'}
        for history, minimum_fit in ((None, 4), ((), 4), ((False,), 4), ((True,), 5),
                                     ((False, True), 5), ((True, False), 4), ((True,) * 3, 5)):
            for fit in range(1, 6):
                for requested in (False, True):
                    with self.subTest(history=history, fit=fit, requested=requested):
                        context = _context()
                        context.state.channel_bot_gifs = history
                        message = context.source_message
                        message.id, message.archive_reply = 300, False
                        message.reply = AsyncMock(return_value=SimpleNamespace(id=301))
                        message.channel.send = AsyncMock(return_value=SimpleNamespace(id=302))
                        message.add_reaction = AsyncMock()
                        session = _AgentSession([social_step('Fallback line.', meme_id='synthetic', fit=fit,
                            gif_requested=requested, form='text_reactions', reactions=['\U0001f44d'])])
                        with (patch.dict(ITEM_BY_ID, synthetic=item),
                              self.assertLogs('elbow_helper.agent.engine.social_reply', level='INFO') as logs):
                            answer = await AgentService(_AgentModel(session, route='social')).answer(
                                question='Synthetic exchange', local_context='', context=context)
                        sent = requested or fit >= minimum_fit
                        self.assertEqual(answer, '' if sent else 'Fallback line.')
                        self.assertEqual(context.state.gif_url, url if sent else None)
                        if sent: self.assertEqual(context.state.gif_fallback_text, 'Fallback line.')
                        self.assertEqual(len(session.calls), 1)
                        self.assertTrue(any(f"'id': 'synthetic', 'fit': {fit}" in line
                                            and f"sent={'synthetic' if sent else None}" in line for line in logs.output))
                        message.add_reaction.assert_awaited_once_with('\U0001f44d')
                        delivery = AgentDelivery()
                        surface = AgentDeliveryMixin()
                        surface._send_response = AsyncMock()
                        with (patch('elbow_helper.agent.delivery.require_evidence_access', AsyncMock()),
                              patch('elbow_helper.agent.delivery.assert_answer_scope', AsyncMock())):
                            await surface.send_response(message, answer, None, context=context, delivery=delivery)
                        self.assertEqual(delivery.text_parts, [url] if sent else [])
                        self.assertEqual(surface._send_response.await_count, int(not sent))
                        if sent: self.assertEqual(message.reply.await_args.args, (url,))

    def test_snapshot_tracks_only_this_bots_replies_and_recognizes_media(self):
        def message(content='', *, author=999, embeds=(), attachments=()):
            return SimpleNamespace(author=SimpleNamespace(id=author), content=content,
                                   embeds=embeds, attachments=attachments)
        gif = 'https://tenor.com/view/synthetic'
        rows = [message(gif), message(gif, author=42), message(gif, author=998),
                message('Synthetic text.'), message('https://[invalid'),
                message(embeds=[SimpleNamespace(type='gifv')]),
                message(attachments=[SimpleNamespace(filename='reaction.GIF')])]
        self.assertEqual(channel_gif_history(rows, 999), (False, True, True))
        self.assertIsNone(channel_gif_history(rows, None))
        self.assertEqual(channel_gif_history([], 999), ())
        self.assertEqual(channel_gif_history(rows + [message('Latest text.')], 999), (True, True, False))
        for media in (message(embeds=[SimpleNamespace(type='image')]),
                      message(attachments=[SimpleNamespace(filename='reaction.png')])):
            with self.subTest(media=media):
                self.assertEqual(channel_gif_history([media, message('Member text.', author=42)], 999),
                                 (True,))
