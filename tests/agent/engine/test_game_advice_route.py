"""The model chooses the remit; no lexical request routing or fixed response."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.engine.routing import ROUTE_INSTRUCTION
from elbow_helper.agent.engine.rounds import SHORT_REPLY_REWRITE_INSTRUCTION
from elbow_helper.agent.engine.service import AgentService, AgentUnavailableError
from elbow_helper.agent.models import RegisteredAgentTool
from elbow_helper.agent.plan.format import answer_instructions
from elbow_helper.agent.prompts import GAME_ADVICE_RULES
from elbow_helper.infrastructure.ai import AgentToolDefinition
from agent.engine.test_agent_plan_flow import _context, _plan
from agent.engine.test_answer_composition import client, provider_response, call


class GameAdviceRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_and_fallback_keep_the_game_remit_with_knowledge_available(self):
        from elbow_helper.agent.knowledge.store import KnowledgeCatalog

        catalog = KnowledgeCatalog('synthetic', ())
        for route in ('', 'research', 'game_advice'):
            for actions_enabled in (False, True):
                with self.subTest(route=route, actions_enabled=actions_enabled):
                    context = replace(_context(),
                        knowledge_store=SimpleNamespace(load=lambda: catalog),
                        bot=SimpleNamespace(tree=object()) if actions_enabled else None)
                    requests = []

                    async def provider(**options):
                        requests.append(deepcopy(options))
                        system = options['messages'][0]['content']
                        if system == ROUTE_INSTRUCTION:
                            return provider_response(route)
                        self.assertIn(GAME_ADVICE_RULES, system)
                        self.assertIn('submit_request_plan', str(options['tools']))
                        return provider_response('That village has seen better days.')

                    model = client(provider)
                    try:
                        with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value={}), \
                             patch('elbow_helper.agent.engine.service.build_command_tools', return_value=({}, {})), \
                             patch('elbow_helper.agent.engine.service.channel_knowledge_access', AsyncMock(return_value=frozenset())):
                            answer = await AgentService(model).answer(
                                question='My village is a museum now.', local_context='', context=context)
                        self.assertEqual(answer, 'That village has seen better days.')
                        self.assertEqual(len(requests), 2)
                    finally:
                        await model.close()

    async def test_advice_route_can_read_approved_faq_before_a_decline(self):
        from elbow_helper.agent.families.reference_reads import TOOLS
        from elbow_helper.agent.knowledge.store import KnowledgeCatalog, KnowledgeSection
        section = KnowledgeSection('Synthetic reward FAQ', 'The earned reward is retained.', file_name='faq.md')
        catalog = KnowledgeCatalog('2026-10-23T00:00:00+00:00', (section,))
        context = replace(_context(), knowledge_store=SimpleNamespace(load=lambda: catalog))
        registry = {'read_community_reference': replace(TOOLS[0], default_visible=True)}
        async def provider(**options):
            system = options['messages'][0]['content']
            if system == ROUTE_INSTRUCTION:
                return provider_response('game_advice')
            self.assertIn(GAME_ADVICE_RULES, system)
            if system == answer_instructions(registry):
                self.assertIn(section.body, str(options['messages']))
                return provider_response('The earned reward is retained.')
            self.assertIn(section.title, str(options['messages']))
            return provider_response(calls=[call('submit_request_plan', _plan([
                {'id': 'faq', 'capability': 'read_community_reference',
                 'arguments': {'reference_kind': 'knowledge', 'titles': [section.title]}, 'depends_on': []}]))])
        model = client(provider)
        try:
            with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry), \
                 patch('elbow_helper.agent.engine.service.channel_knowledge_access', AsyncMock(return_value=frozenset())):
                answer = await AgentService(model).answer(question='Does leaving change my reward?',
                                                          local_context='', context=context)
            self.assertEqual(answer, section.body)
            self.assertTrue(context.state.knowledge_sources)
        finally:
            await model.close()

    async def test_truncated_social_choice_closes_call_before_rewrite(self):
        requests = []

        async def provider(**options):
            requests.append(deepcopy(options))
            if options['messages'][0]['content'] == ROUTE_INSTRUCTION:
                return provider_response('social')
            retry = len(requests) > 2
            response = provider_response(calls=[call('submit_social_reply', {
                'mechanic_check': 'no_gameplay_number', 'reply_form': 'text',
                'reaction_fit': False, 'reaction_basis': '',
                'text': 'Complete social line.' if retry else 'Discard this draft',
                'meme_shortlist': [{'id': item, 'fit': 1} for item in
                    ('ts_lowkey_tuff', 'things_will_happen_horse', 'son_not_tuff')],
                'gif_requested': False, 'reactions': [],
            })])
            if not retry:
                response.choices[0].finish_reason = 'length'
            return response

        model = client(provider)
        context = _context()
        try:
            answer = await AgentService(model).answer(question='Synthetic banter.',
                local_context='', context=context)
            self.assertEqual(answer, 'Complete social line.')
            self.assertEqual(len(requests), 3)
            feedback = [message for message in requests[-1]['messages']
                        if isinstance(message, dict) and message['role'] == 'tool']
            self.assertEqual(len(feedback), 1)
            self.assertEqual(feedback[0]['tool_call_id'], 'synthetic-call')
            self.assertIn(SHORT_REPLY_REWRITE_INSTRUCTION, feedback[0]['content'])
            self.assertFalse(context.state.gif_url)
            self.assertFalse(context.state.reactions.emojis)
        finally:
            await model.close()

    async def test_short_routes_rewrite_truncated_drafts_without_appending_them(self):
        for route in ('game_advice', 'social'):
            for retry_truncated in (False, True):
                with self.subTest(route=route, retry_truncated=retry_truncated):
                    requests = []

                    async def provider(**options):
                        requests.append(deepcopy(options))
                        if options['messages'][0]['content'] == ROUTE_INSTRUCTION:
                            return provider_response(route)
                        response = provider_response('Fresh complete reply.' if len(requests) > 2
                                                     else 'Unfinished synthetic draft')
                        if len(requests) == 2 or retry_truncated:
                            response.choices[0].finish_reason = 'length'
                        return response

                    model = client(provider)
                    try:
                        context = _context()
                        request = AgentService(model).answer(question='Synthetic exchange.',
                                                            local_context='', context=context)
                        if retry_truncated:
                            with self.assertRaises(AgentUnavailableError):
                                await request
                            self.assertEqual(context.state.terminal_cause, 'limit')
                        else:
                            self.assertEqual(await request, 'Fresh complete reply.')
                        self.assertEqual(len(requests), 3)
                        self.assertIn(SHORT_REPLY_REWRITE_INSTRUCTION,
                                      str(requests[-1]['messages']))
                        self.assertEqual(requests[-1]['max_tokens'], 1024)
                    finally:
                        await model.close()

    async def test_model_classified_advice_has_no_tools_or_reference_loading(self):
        for wording in ('Synthetic grumble one.', 'Synthetic grumble two.'):
            requests = []
            context = _context()

            async def provider(**options):
                requests.append(deepcopy(options))
                if options['messages'][0]['content'] == ROUTE_INSTRUCTION:
                    return provider_response('game_advice')
                self.assertIn(GAME_ADVICE_RULES, options['messages'][0]['content'])
                self.assertEqual(options['tool_choice'], 'none')
                self.assertNotIn('tools', options)
                self.assertLessEqual(options['max_tokens'], 1024)
                return provider_response(wording)

            model = client(provider)
            try:
                with patch('elbow_helper.agent.engine.service.build_agent_tools') as tools:
                    answer = await AgentService(model).answer(question='Would this arrangement work?',
                        local_context='Synthetic pictured build discussion.', context=context)
                self.assertEqual(answer, wording)
                self.assertEqual(len(requests), 2)
                tools.assert_not_called()
                self.assertFalse(context.state.gif_url)
                self.assertFalse(context.state.reactions.emojis)
            finally:
                await model.close()

    async def test_model_classified_war_data_keeps_the_normal_read_path(self):
        source = AsyncMock(return_value={'rows': [{'name': 'Vega', 'attacks': 4}]})
        registry = {'read_sample': RegisteredAgentTool(AgentToolDefinition('read_sample',
            'Read synthetic war records.', {'type': 'object', 'properties': {}}), source)}

        async def provider(**options):
            system = options['messages'][0]['content']
            if system == ROUTE_INSTRUCTION:
                return provider_response('research')
            if system == answer_instructions(registry):
                return provider_response('Vega used four attacks.')
            return provider_response(calls=[call('submit_request_plan', _plan([
                {'id': 'war', 'capability': 'read_sample', 'arguments': {}, 'depends_on': []}]))])

        model = client(provider)
        try:
            with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
                answer = await AgentService(model).answer(question='How many spell attacks did Vega use?',
                    local_context='', context=_context())
            self.assertEqual(answer, 'Vega used four attacks.')
            source.assert_awaited_once()
        finally:
            await model.close()
