"""Stored-data SQL is discovered with its guide and checked reader access."""
import unittest
from unittest.mock import Mock, patch

from elbow_helper.agent.engine.service import AgentService
from agent.engine.test_agent_plan_flow import _context, _Model, _Session


def _fixed_prompt():
    model = _Model(_Session([], []))
    AgentService(model)._start_flow(question='Synthetic request', local_context='',
                                    context=_context(), conversation_history='')
    return model.request['system_prompt']


def _capability_names(prompt):
    block = prompt.split('<capabilities>')[1].split('</capabilities>')[0]
    return {line.split(':')[0].split(' |')[0] for line in block.splitlines()}


class DataQueryCatalogueTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_with_access_discovers_the_data_query_after_family_reads(self):
        names = _capability_names(_fixed_prompt())
        self.assertNotIn('query_bot_data', names)
        self.assertIn('read_member_cohort', names)
        self.assertIn('open_capability_group', names)
        session = _Session([], [])
        session.replace_tools = Mock()
        context = _context()
        flow, _ = AgentService(_Model(session))._start_flow(question='Synthetic request', local_context='',
                                                          context=context, conversation_history='')
        await flow.registry['open_capability_group'].handler(context, {'group': 'records', 'intent': 'inspect'})
        self.assertIn('query_bot_data', flow.registry)

    async def test_reader_without_access_does_not_discover_it(self):
        session = _Session([], [])
        session.replace_tools = Mock()
        context = _context()
        flow, _ = AgentService(_Model(session))._start_flow(question='Synthetic request', local_context='',
                                                          context=context, conversation_history='')
        with patch('elbow_helper.agent.families.catalogue.has_lookup_access', return_value=False):
            result = await flow.registry['open_capability_group'].handler(
                context, {'group': 'records', 'intent': 'inspect'})
        self.assertNotIn('query_bot_data', flow.registry)
        self.assertNotIn('query_bot_data', result['catalogue'])
        self.assertIn('read_member_cohort', flow.registry)

    def test_instruction_names_the_long_period_use_and_the_guide_route(self):
        prompt = _fixed_prompt()
        self.assertIn('trends or comparisons over long periods or many accounts that no family read '
                      'returns, use query_bot_data', prompt)
        self.assertIn('call open_capability_group', prompt)
        self.assertIn('open group records with intent inspect', prompt)
        self.assertIn('for its table guide', prompt)
        self.assertIn('Family reads stay first for everything they cover', prompt)


if __name__ == '__main__':
    unittest.main()
