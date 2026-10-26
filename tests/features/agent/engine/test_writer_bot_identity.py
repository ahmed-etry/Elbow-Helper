"""Verified bot responsibility is factual writer input, independent of policy reads."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest

from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.models import AgentIdentity
from features.agent.engine.test_agent_plan_flow import _context
from features.agent.engine.test_planner_fix2_evidence import WriterModel


class WriterBotIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_identity_reaches_both_writer_modes_with_live_guild_name(self):
        for allow_more in (False, True):
            context = replace(_context(), application_owner=AgentIdentity('Global builder', 77))
            original = context.guild.get_member
            context.guild.get_member = lambda key: (
                SimpleNamespace(id=77, display_name='Guild builder') if key == 77 else original(key))
            model = WriterModel()
            flow, _ = AgentService(model)._start_flow(question='Identify the responsible person.',
                local_context='', context=context, conversation_history='')
            captured = []
            flow.final_session_factory = lambda **kwargs: (captured.append(kwargs) or object())
            self.assertTrue(await flow._prepare_answer_session(allow_more=allow_more))
            evidence = json.loads(captured[-1]['prompt'].split('\nAvailable evidence:\n', 1)[1])
            self.assertEqual(evidence['results']['bot_identity']['owner_and_operator'],
                             {'display_name': 'Guild builder', 'member_id': 77})
            self.assertFalse(context.state.required_access)
            self.assertFalse(context.state.proposed_changes)

    async def test_unknown_owner_is_not_inferred_from_guild_owner_or_requester(self):
        context, model = _context(), WriterModel()
        context.guild.owner_id = 41
        flow, _ = AgentService(model)._start_flow(question='Identify the responsible person.',
            local_context='', context=context, conversation_history='')
        self.assertTrue(await flow._prepare_answer_session())
        evidence = json.loads(model.writer['prompt'].split('\nAvailable evidence:\n', 1)[1])
        self.assertNotIn('bot_identity', evidence['results'])
