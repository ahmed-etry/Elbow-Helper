"""Different requests share the complete fixed instruction and catalogue prefix."""
import unittest

from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.routing import RouteDecision
from tests.agent.engine.test_agent_plan_flow import _context, _Model, _Session
from tests.agent.discord_actions.test_agent_reactions import _server_emoji


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
