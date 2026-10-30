"""Ability answers consult every registered tool without enlarging normal prompts."""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import discord

from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.engine.routing import RouteDecision
from elbow_helper.agent.engine.self_knowledge import (
    ability_claim, capability_context, capability_index,
)
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage
from agent.engine.helpers import social_step
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
    def test_every_registered_tool_has_exactly_one_deterministic_line(self):
        registry, context = build_agent_tools(), _context()
        entries = capability_index(registry, context)
        self.assertEqual(set(entries), set(registry))
        self.assertEqual(entries, capability_index(dict(reversed(list(registry.items()))), context))
        text = capability_context(registry, context)
        self.assertEqual(sum(line.startswith('- ') for line in text.splitlines()), len(registry))
        for name in registry:
            self.assertNotIn(name, text)
        # New tools are covered without a separately maintained description list.
        synthetic = replace(next(iter(registry.values())),
            definition=replace(next(iter(registry.values())).definition,
                               name='synthetic_future_tool', description='Future supported work.'))
        entries = capability_index({**registry, 'synthetic_future_tool': synthetic}, context)
        self.assertIn('synthetic_future_tool', entries)

    def test_hidden_denied_tools_stay_in_index_without_reading_restricted_sources(self):
        registry, context = build_agent_tools(), _context()
        context.member.guild_permissions = discord.Permissions.none()
        before = repr(context.state)
        entries = capability_index(registry, context)
        self.assertIn('needs Manage Roles', entries['change_channel_permissions'][1])
        self.assertIn('requester guild permission=false', entries['change_channel_permissions'][1])
        self.assertIn('needs Lead; requester lacks access', entries['read_role_connections'][1])
        self.assertIn('add_discord_roles', entries)
        self.assertEqual(repr(context.state), before)

    def test_permission_changes_are_checked_against_current_membership(self):
        registry, context = build_agent_tools(), _context()
        context.member.guild_permissions = discord.Permissions(manage_roles=True)
        self.assertIn('requester guild permission=true',
                      capability_index(registry, context)['change_channel_permissions'][1])
        context.guild.get_member = lambda _: None
        self.assertIn('requester guild permission=false',
                      capability_index(registry, context)['change_channel_permissions'][1])

    def test_command_role_limits_are_included_without_adding_executable_tools(self):
        registry, context = build_agent_tools(), _context()
        context.guild.roles.append(SimpleNamespace(id=505, name='Trial operator'))
        name = next(iter(registry))
        entries = capability_index(registry, context,
            {name: SimpleNamespace(visible_to=frozenset({505}))})
        self.assertIn('needs Trial operator; requester lacks access', entries[name][1])

    def test_claim_detection_covers_plain_lists_negatives_and_reply_plans(self):
        for text in ("No, that's not something I do.", 'I can edit channel permissions.',
                     'I cannot change channels.', 'I can\u2019t manage roles.', 'No.'):
            with self.subTest(text=text):
                self.assertTrue(ability_claim(reply(text), QUESTION))
        self.assertTrue(ability_claim(reply('- War results\n- Role changes'), 'What can you do?'))
        plan = {'steps': [], 'reply': "That's not something I do."}
        step = AgentStep('', (AgentToolCall('plan', 'submit_request_plan', json.dumps(plan)),), AgentUsage())
        self.assertTrue(ability_claim(step, QUESTION))
        # An acknowledgement beside real work is discarded by the planner.
        plan['steps'] = [{'capability': 'read_discord_access'}]
        step = replace(step, tool_calls=(replace(step.tool_calls[0], arguments=json.dumps(plan)),))
        self.assertFalse(ability_claim(step, QUESTION))


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

    async def test_overview_list_reads_full_index_even_without_first_person_wording(self):
        session = _Session([reply('- War results\n- Rosters\n- Channel permission changes')], [])
        flow, _, _ = start(session, question='What can you do?')
        self.assertIn('Channel permission changes', await flow.run())
        self.assertIn('channel permission overwrites', session.instructions[0])
        self.assertEqual(len(session.calls), 1)

    async def test_completed_lookups_keep_offers_and_data_gaps_without_extra_tokens(self):
        for text in ("BE4 is the CWL clan. I can compare its seasons.",
                     "The roster isn't available, so I can't rank its players.",
                     "I couldn't confirm the requested count."):
            with self.subTest(text=text):
                session = _Session([reply(text)], [])
                flow, _, _ = start(session, question='Compare the clans.')
                flow.rounder.results_available = True
                flow.rounder.capability_context = Mock(side_effect=AssertionError('Unexpected catalogue read'))
                self.assertEqual((await flow.rounder.advance()).content, text)
                self.assertEqual(session.instructions, [None])

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

    async def test_ordinary_lookup_keeps_one_round_and_no_added_instruction(self):
        session = _Session([reply('The clan has 40 members.')], [])
        flow, _, _ = start(session, question='Can you show the clan member count?')
        flow.rounder.capability_context = Mock(side_effect=AssertionError('Unexpected catalogue read'))
        self.assertEqual(await flow.run(), 'The clan has 40 members.')
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.instructions, [None])

    async def test_banter_keeps_fixed_prompt_bytes_and_one_round(self):
        context = _context()
        session = _Session([social_step('I can roast harder than that.')], [])
        model = _Model(session)
        frozen = SimpleNamespace(now=lambda tz: datetime(2026, 10, 26, tzinfo=timezone.utc))
        with patch('elbow_helper.agent.engine.service.datetime', frozen):
            flow, _ = AgentService(model)._start_flow(question="You're a menace",
                local_context='Synthetic banter.', context=context, conversation_history='',
                route=RouteDecision(social=True))
        # Captured with fixed instructions before request data. Update only for
        # an intentional instruction or prompt-layout change.
        expected = {
            'prompt': 'a5f81ffb1f888da2716cfe22adc23dbf5c475b3ea71d53c3a1617e11aa5f1a1b',
            'system_prompt': '917a056119203c3eb7bda776c346edeaa6969c52413df238364cfe5321cd6e28',
        }
        for key, digest in expected.items():
            self.assertEqual(hashlib.sha256(model.request[key].encode()).hexdigest(), digest)
        flow.rounder.capability_context = Mock(side_effect=AssertionError('Unexpected catalogue read'))
        await flow.rounder.advance()
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.instructions, [None])

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
