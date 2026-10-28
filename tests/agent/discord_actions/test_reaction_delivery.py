"""Reactions require message delivery; pending replies retain their delivery state."""

import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.access import AgentAccessLost
from elbow_helper.agent.delivery import AgentDeliveryMixin
from elbow_helper.agent.models import AgentDelivery
from agent.engine.test_agent_service import _context


class ReactionDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reaction_acknowledgement_requires_message_delivery(self):
        context = _context()
        context.state.reactions.emojis.append("\U0001f44d")
        surface = AgentDeliveryMixin()
        surface._send_response = AsyncMock()
        delivery = AgentDelivery()
        await surface.send_response(context.source_message, "", None, context=context, delivery=delivery)
        surface._send_response.assert_awaited_once()
        self.assertFalse(delivery.complete)
        self.assertEqual(delivery.message_ids, [])
        self.assertEqual(delivery.attempted_nonces, [])

    async def test_missing_reaction_or_pending_output_does_not_claim_silent_completion(self):
        for pending in ("no_reaction", "text", "attachments", "proposed_changes", "preview_reply", "outcomes"):
            with self.subTest(pending=pending):
                context = _context()
                if pending != "no_reaction":
                    context.state.reactions.emojis.append("\U0001f44d")
                if pending in ("proposed_changes", "outcomes"):
                    getattr(context.state, pending).append(object())
                if pending == "preview_reply":
                    context.state.preview_reply = "Synthetic preview"
                surface = AgentDeliveryMixin()
                surface._send_response = AsyncMock()
                delivery = AgentDelivery()
                await surface.send_response(
                    context.source_message, "Synthetic text" if pending == "text" else "", None,
                    attachments=(object(),) if pending == "attachments" else (),
                    context=context, delivery=delivery,
                )
                surface._send_response.assert_awaited_once()
                self.assertFalse(delivery.complete)

    async def test_lost_access_does_not_mark_delivery_complete(self):
        context = _context()
        context.state.reactions.emojis.append("\U0001f44d")
        context.guild.get_member = lambda _: None
        delivery = AgentDelivery()
        with self.assertRaises(AgentAccessLost):
            await AgentDeliveryMixin().send_response(context.source_message, "", None,
                                                    context=context, delivery=delivery)
        self.assertFalse(delivery.complete)
