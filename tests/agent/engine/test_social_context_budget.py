"""Social replies avoid research context until they actually enter research."""
import unittest

from elbow_helper.agent.engine.routing import RouteDecision
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.catalogue import FAMILY_INSTRUCTION
from elbow_helper.agent.engine.prompts import FACT_RULES, RESEARCH_RULES
from tests.agent.engine.test_agent_service import _AgentModel, _AgentSession, _context


class SocialContextTests(unittest.IsolatedAsyncioTestCase):
    def flow(self, *, social):
        context = _context()
        context.member.display_name = 'Synthetic member'
        model = _AgentModel(_AgentSession([]))
        flow, _ = AgentService(model)._start_flow(question='Synthetic exchange', local_context='',
            context=context, conversation_history='', route=RouteDecision(social=social))
        return flow, model


    async def test_expansion_restores_the_research_catalogue_and_command_metadata(self):
        flow, _ = self.flow(social=True)
        instructions = await flow.rounder.expand_context()
        self.assertIn(FAMILY_INSTRUCTION, instructions)
        self.assertIn('<bot_command_overview>', instructions)
        self.assertIn(FACT_RULES, instructions)
        self.assertIn(RESEARCH_RULES, instructions)
        self.assertIn('bot_command_overview', flow.answer_context)
        self.assertIn('requester_command_overview', flow.answer_context)
        self.assertIn('bot_read_overview', flow.answer_context)
        self.assertFalse(flow.rounder.social_route)
