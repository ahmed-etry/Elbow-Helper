"""Ability answers consult every registered tool without enlarging normal prompts."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import discord

from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.engine.routing import RouteDecision
from elbow_helper.agent.engine.self_knowledge import capability_index
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage
from agent.engine.test_agent_plan_flow import _context, _Model, _Session


QUESTION = "Can you edit channel permissions for a channel and clean up a category's permissions?"


def reply(text):
    return AgentStep(text, (), AgentUsage())


def start(session, *, question=QUESTION, social=False, bot=True):
    context = _context()
    context.member.guild_permissions = discord.Permissions.none()
    if bot:
        context = replace(context, bot=SimpleNamespace(tree=object()))
    model = _Model(session)
    with patch('elbow_helper.agent.engine.service.build_command_tools', return_value=({}, {})):
        flow, _ = AgentService(model)._start_flow(question=question, local_context='',
            context=context, conversation_history='', route=RouteDecision(social=social))
    return flow, model, context


class CapabilityIndexTests(unittest.TestCase):


    def test_permission_changes_are_checked_against_current_membership(self):
        registry, context = build_agent_tools(), _context()
        context.member.guild_permissions = discord.Permissions(manage_roles=True)
        self.assertIn('requester guild permission=true',
                      capability_index(registry, context)['change_channel_permissions'][1])
        context.guild.get_member = lambda _: None
        self.assertIn('requester guild permission=false',
                      capability_index(registry, context)['change_channel_permissions'][1])


class SelfKnowledgeFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_capability_refusal_is_rewritten_after_reading_full_index(self):
        expected = 'Yes, I can change channel and category permissions. That needs Manage Roles.'
        session = _Session([reply("No, that's not something I do."), reply(expected)], [])
        flow, _, context = start(session)
        registry_before = dict(flow.registry)
        self.assertNotIn('change_channel_permissions', flow.registry)
        self.assertEqual(await flow.run(), expected)
        index = session.instructions[1]
        self.assertIn('<complete_capability_index>', index)
        self.assertIn('Set, change or remove channel permission overwrites.', index)
        self.assertIn('Rename, move or change topic and slowmode.', index)
        self.assertIn('needs Manage Roles', index)
        self.assertIn('requester guild permission=false', index)
        self.assertEqual(flow.registry, registry_before)
        self.assertFalse(context.state.proposed_changes)
        self.assertEqual(len(session.calls), 2)


    async def test_reply_plan_is_acknowledged_and_not_executed_before_rewrite(self):
        plan = {'goal': 'Describe capabilities', 'effort': 'low', 'output': 'text',
                'steps': [], 'reply': 'I cannot change channel permissions.'}
        draft = AgentStep('', (AgentToolCall('draft', 'submit_request_plan', json.dumps(plan)),), AgentUsage())
        session = _Session([draft, reply('That needs Manage Roles.')], [])
        flow, _, context = start(session)
        self.assertEqual(await flow.run(), 'That needs Manage Roles.')
        self.assertEqual(session.calls[1][0][0].call_id, 'draft')
        self.assertIn('not executed', session.calls[1][0][0].content)
        self.assertFalse(context.state.proposed_changes)


    async def test_new_writer_session_receives_index_before_its_first_reply(self):
        session = _Session([reply('I cannot do that.'), reply('That needs Manage Roles.')], [])
        flow, _, context = start(session)
        await flow.rounder.advance()
        # A fresh writer has no planning transcript, and permissions may change.
        context.member.guild_permissions = discord.Permissions(manage_roles=True)
        writer = _Session([reply('I can help with that.')], [])
        flow.rounder.session = writer
        await flow.rounder.advance(allow_tools=False)
        self.assertIn('<complete_capability_index>', writer.instructions[0])
        self.assertIn('requester guild permission=true', writer.instructions[0])
        self.assertEqual(len(writer.calls), 1)

    async def test_final_only_reply_can_be_corrected_within_existing_budget(self):
        session = _Session([reply('I cannot do that.'), reply('That needs Manage Roles.')], [])
        flow, _, _ = start(session)
        step = await flow.rounder.advance(allow_tools=False, final=True)
        self.assertEqual(step.content, 'That needs Manage Roles.')
        self.assertTrue(flow.rounder.final_attempted)
        self.assertEqual(len(session.calls), 2)
