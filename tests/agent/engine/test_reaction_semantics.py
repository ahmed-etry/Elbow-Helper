"""Model semantic decisions must authorize optional reactions before execution."""

import json
import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.engine.service import AgentService
from agent.engine.helpers import social_step
from agent.engine.test_agent_service import _AgentModel, _AgentSession, _context


class ReactionSemanticTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_missing_or_unexplained_fit_keeps_text_without_reacting(self):
        for fields in ({'reaction_fit': False}, {'reaction_basis': ''},
                       {'reaction_basis': '  '}, {'reaction_fit': 'true'}):
            with self.subTest(fields=fields):
                context = _context()
                context.source_message.id = 300
                context.source_message.add_reaction = AsyncMock()
                session = _AgentSession([social_step('Synthetic reply.', form='text_reactions',
                    meme_id=None, reactions=['\U0001f44d'], **fields)])
                answer = await AgentService(_AgentModel(session, route='social')).answer(
                    question='Synthetic exchange.', local_context='', context=context)
                self.assertEqual(answer, 'Synthetic reply.')
                context.source_message.add_reaction.assert_not_awaited()

    async def test_structured_fit_is_internal_and_matching_reaction_executes(self):
        context = _context()
        context.source_message.id = 300
        context.source_message.add_reaction = AsyncMock()
        step = social_step('Synthetic reply.', form='text_reactions', meme_id=None,
                           reactions=['\U0001f44d'])
        reason = json.loads(step.tool_calls[0].arguments)['reaction_basis']
        session = _AgentSession([step])
        answer = await AgentService(_AgentModel(session, route='social')).answer(
            question='Synthetic exchange.', local_context='', context=context)
        context.source_message.add_reaction.assert_awaited_once_with('\U0001f44d')
        self.assertNotIn(reason, answer)
        self.assertEqual(len(session.calls), 1)
