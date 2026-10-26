"""Different requests share the complete fixed instruction and catalogue prefix."""
from dataclasses import replace
from types import SimpleNamespace
import unittest

from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.routing import RouteDecision
from elbow_helper.features.agent.knowledge.store import KnowledgeCatalog, KnowledgeSection
from features.agent.engine.test_agent_plan_flow import _context, _Model, _Session
from features.agent.discord_actions.test_agent_reactions import _server_emoji


class PromptPrefixTests(unittest.TestCase):
    def test_reaction_selection_context_is_only_supplied_to_social_replies(self):
        for route in (RouteDecision(), RouteDecision(social=True), RouteDecision(game_advice=True)):
            with self.subTest(route=route):
                context = _context()
                context.guild.emojis = (_server_emoji('bigbrain', 101),)
                context.state.channel_reactions.emojis = ('<:syntheticWave:102>',)
                model = _Model(_Session([], []))
                AgentService(model)._start_flow(question='Synthetic exchange.', local_context='',
                    context=context, conversation_history='', route=route)
                for value in ('Available server reaction emojis and meanings:', '<:bigbrain:101>',
                              'Previous bot reaction in this channel:', '<:syntheticWave:102>'):
                    if route.social:
                        self.assertIn(value, model.request['prompt'])
                    else:
                        self.assertNotIn(value, model.request['prompt'])

    def test_requests_keep_indexes_locators_and_history_after_the_same_fixed_prefix(self):
        requests = []
        for number in (1, 2):
            context = _context()
            context.source_message.id = 100 + number
            context.state.channel_directory = f'Channel locator {number}'
            catalog = KnowledgeCatalog('synthetic', (KnowledgeSection(
                f'Heading {number}', f'Unselected body {number}', file_name=f'{number}.md'),)).prepare_prompts()
            context = replace(context, knowledge_store=SimpleNamespace(load=lambda: catalog))
            model = _Model(_Session([], []))
            flow, _ = AgentService(model)._start_flow(question=f'Synthetic request {number}',
                local_context=f'Nearby message {number}', context=context,
                conversation_history=f'Authorized history {number}')
            request = model.request
            self.assertIn('open_capability_group', request['system_prompt'])
            for value in (f'Channel locator {number}', f'Heading {number}', f'Nearby message {number}',
                          f'Authorized history {number}', f'Synthetic request {number}'):
                self.assertNotIn(value, request['system_prompt'])
                self.assertIn(value, request['prompt'])
            self.assertNotIn(f'Unselected body {number}', request['system_prompt'] + request['prompt'])
            self.assertEqual(flow.fixed_system_prompt, request['system_prompt'])
            requests.append(request)
        self.assertEqual(requests[0]['system_prompt'].encode(), requests[1]['system_prompt'].encode())
        self.assertEqual(requests[0]['tools'], requests[1]['tools'])
        self.assertNotEqual(requests[0]['prompt'], requests[1]['prompt'])
