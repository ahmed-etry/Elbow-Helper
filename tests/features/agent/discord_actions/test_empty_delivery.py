"""An empty final answer must still produce a tracked Discord reply."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.models import AgentDelivery


class EmptyDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_and_whitespace_answers_deliver_fallback(self):
        for response in ("", " \n\t "):
            with self.subTest(response=response):
                surface = AgentDeliveryMixin()
                surface.bot = SimpleNamespace(user=None)
                sent = SimpleNamespace(id=301)
                message = SimpleNamespace(
                    id=300, channel=SimpleNamespace(id=100), mentions=[],
                    reply=AsyncMock(return_value=sent), archive_reply=False,
                )
                delivery = AgentDelivery()
                await surface.send_response(message, response, None, delivery=delivery)
                self.assertEqual(message.reply.await_args.args[0], "What result do you need?")
                self.assertEqual(delivery.text_parts, ["What result do you need?"])
                self.assertEqual(delivery.message_ids, [301])
                self.assertTrue(delivery.complete)
