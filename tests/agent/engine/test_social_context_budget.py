"""Social replies avoid research context until they actually enter research."""
import unittest

from elbow_helper.agent.engine.routing import RouteDecision
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.families.catalogue import FAMILY_INSTRUCTION
from elbow_helper.agent.prompts import FACT_RULES, RESEARCH_RULES
from agent.engine.test_agent_service import _AgentModel, _AgentSession, _context


class SocialContextTests(unittest.IsolatedAsyncioTestCase):
    def flow(self, *, social):
        context = _context()
        context.member.display_name = 'Synthetic member'
        model = _AgentModel(_AgentSession([]))
        flow, _ = AgentService(model)._start_flow(question='Synthetic exchange', local_context='',
            context=context, conversation_history='', route=RouteDecision(social=social))
        return flow, model

    def test_social_session_omits_unusable_research_instructions_and_command_metadata(self):
        _, social = self.flow(social=True)
        _, research = self.flow(social=False)
        self.assertNotIn(FAMILY_INSTRUCTION, social.request['system_prompt'])
        self.assertIn(FAMILY_INSTRUCTION, research.request['system_prompt'])
        self.assertNotIn('<bot_command_overview>', social.request['prompt'])
        self.assertIn('<bot_command_overview>', research.request['prompt'])
        self.assertLess(len(social.request['system_prompt']) + len(social.request['prompt']),
                        len(research.request['system_prompt']) + len(research.request['prompt']))

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
