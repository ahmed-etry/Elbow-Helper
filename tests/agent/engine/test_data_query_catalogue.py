"""The stored-data query is in the first catalogue for readers who may use it."""
import unittest
from unittest.mock import patch

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


class DataQueryCatalogueTests(unittest.TestCase):
    def test_reader_with_access_sees_the_data_query_beside_family_reads(self):
        names = _capability_names(_fixed_prompt())
        self.assertIn('query_bot_data', names)
        self.assertIn('read_member_cohort', names)
        self.assertIn('open_capability_group', names)

    def test_reader_without_access_does_not_see_it(self):
        with patch('elbow_helper.agent.engine.service.has_lookup_access', return_value=False):
            prompt = _fixed_prompt()
        names = _capability_names(prompt)
        self.assertNotIn('query_bot_data', names)
        self.assertIn('read_member_cohort', names)

    def test_instruction_names_the_long_period_use_and_the_guide_route(self):
        prompt = _fixed_prompt()
        self.assertIn('trends or comparisons over long periods or many accounts that no family read '
                      'returns, use query_bot_data; open group records with intent inspect first '
                      'for its table guide', prompt)
        self.assertIn('Family reads stay first for everything they cover', prompt)


if __name__ == '__main__':
    unittest.main()
