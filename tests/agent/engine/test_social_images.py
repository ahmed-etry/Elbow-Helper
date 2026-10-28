"""Visual observations reach social composition within the existing routing call."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord

from elbow_helper.agent.engine.routing import judge_route
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage, TextGenerationError
from elbow_helper.infrastructure.ai.agent import AgentImage
from agent.files.image_helpers import png
from agent.engine.helpers import social_step
from agent.engine.test_agent_service import _AgentModel, _AgentSession, _context


class SocialImageTests(unittest.IsolatedAsyncioTestCase):
    def context(self, kind):
        context = _context()
        source = context.source_message
        source.id, source.content, source.author = 300, "A square for our emoji drawer?", context.member
        source.attachments, source.stickers, source.embeds = [], [], []
        media = source
        sources = (source,)
        if kind == 'reply':
            media = SimpleNamespace(id=299, channel=source.channel, author=context.member,
                                    content="", attachments=[], stickers=[], embeds=[])
            source.reference = SimpleNamespace(message_id=299, resolved=media)
            source.channel.fetch_message = AsyncMock(return_value=media)
            sources = (source, media)
        download = AsyncMock(return_value=png('red'))
        if kind == 'sticker':
            media.stickers = [SimpleNamespace(name='Synthetic square',
                                             format=discord.StickerFormatType.png, read=download)]
        else:
            media.attachments = [SimpleNamespace(filename='square.png', content_type='image/png',
                                                size=len(png('red')), read=download)]
        return replace(context, attachment_sources=sources), download

    async def test_current_replied_images_and_stickers_are_read_before_social_composition(self):
        for kind in ('attachment', 'sticker', 'reply'):
            with self.subTest(kind=kind):
                context, download = self.context(kind)
                observation = 'Image 1 from the supplied sender is a solid red square.'
                session = _AgentSession([social_step('Solid square energy.', meme_id=None)])
                model = _AgentModel(session, route=json.dumps({
                    'route': 'social', 'image_observation': observation,
                }))
                answer = await AgentService(model).answer(
                    question=context.source_message.content, local_context='', context=context,
                )
                self.assertEqual(answer, 'Solid square energy.')
                download.assert_awaited_once()
                self.assertEqual(len(model.routing_request['images']), 1)
                image, = model.routing_request['images']
                self.assertEqual(image.data, png('red'))
                self.assertIn('"sender"', image.label)
                self.assertEqual(model.request['images'], model.routing_request['images'])
                self.assertIn(observation, model.request['prompt'])
                self.assertIn('"status": "observed"', model.request['prompt'])
                self.assertEqual(len(model.routing_session.calls), 1)
                self.assertEqual(len(session.calls), 1)

    async def test_missing_observation_is_explicit_without_an_extra_model_call(self):
        context, _ = self.context('attachment')
        session = _AgentSession([social_step('I cannot make out that image.', meme_id=None)])
        model = _AgentModel(session, route=json.dumps({'route': 'social', 'image_observation': ''}))
        answer = await AgentService(model).answer(
            question=context.source_message.content, local_context='', context=context,
        )
        self.assertEqual(answer, 'I cannot make out that image.')
        self.assertIn('"status": "unavailable"', model.request['prompt'])
        self.assertEqual(len(model.routing_session.calls) + len(session.calls), 2)

    async def test_malformed_truncated_or_failed_reads_do_not_supply_visual_evidence(self):
        image = AgentImage('image/png', png('red'), 'Image 1')
        for reply in (
            AgentStep('{"route":"social","image_observation":[]}', (), AgentUsage()),
            AgentStep(json.dumps({'route': 'social', 'image_observation': 'x' * 1201}), (), AgentUsage()),
            AgentStep('{"route":"social",', (), AgentUsage()),
            AgentStep('{"route":"social","image_observation":"red"}', (), AgentUsage(),
                      output_limit_reached=True),
            TextGenerationError('Synthetic image rejection'),
            TimeoutError(),
        ):
            with self.subTest(reply=reply):
                advance = AsyncMock(side_effect=reply) if isinstance(reply, Exception) else AsyncMock(return_value=reply)
                model = Mock(create_agent_session=Mock(return_value=SimpleNamespace(advance=advance)))
                route = await judge_route(model, question='React to this shape', local_context='',
                                          conversation_history='', context=_context(), images=(image,))
                self.assertFalse(route.social)
                self.assertEqual(route.image_observation, '')
                advance.assert_awaited_once()

    async def test_plain_text_social_requests_keep_two_calls_without_visual_context(self):
        session = _AgentSession([social_step('Evening.', meme_id=None)])
        model = _AgentModel(session, route='social')
        await AgentService(model).answer(question='Hello there', local_context='', context=_context())
        self.assertNotIn('images', model.routing_request)
        self.assertNotIn('<image_observation>', model.request['prompt'])
        self.assertEqual(len(model.routing_session.calls) + len(session.calls), 2)


if __name__ == '__main__':
    unittest.main()
