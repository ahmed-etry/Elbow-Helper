"""Stored-data SQL is discovered with its guide and checked reader access."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.models import EvidenceScope
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.engine.catalogue import discovery_tool
from tests.agent.engine.test_agent_plan_flow import _context, _Model, _Session


def _fixed_prompt():
    model = _Model(_Session([], []))
    AgentService(model)._start_flow(question='Synthetic request', local_context='',
                                    context=_context(), conversation_history='')
    return model.request['system_prompt']


def _capability_names(prompt):
    block = prompt.split('<capabilities>')[1].split('</capabilities>')[0]
    return {line.split(':')[0].split(' |')[0] for line in block.splitlines()}


class DataQueryCatalogueTests(unittest.IsolatedAsyncioTestCase):
    def test_transfer_queue_is_named_in_the_initial_group_index(self):
        prompt = _fixed_prompt()
        self.assertIn('transfers:read_pending_transfer_requests', prompt)
        self.assertNotIn('read_pending_transfer_requests', _capability_names(prompt))

    async def test_shared_audit_discovery_distinguishes_private_access_without_revealing_it_to_other_requesters(self):
        context = _context()
        context.state.scope = EvidenceScope(91, frozenset(), frozenset())
        context.member.guild_permissions = SimpleNamespace(view_audit_log=True)
        context.guild.me.guild_permissions = SimpleNamespace(view_audit_log=True)
        tools = build_agent_tools()
        target = 'read_discord_audit_log'
        active = {}
        tool = discovery_tool(context, tools, active, lambda: None)
        withheld = {'cause': 'scope_refused', 'available_privately': True}
        index = tool.definition.parameters['properties']['group']['description'].splitlines()
        self.assertEqual(json.loads(index[-1].removeprefix('withheld:'))[target], withheld)
        self.assertNotIn(target, ''.join(index[:-1]))
        opened = await tool.handler(context, {'group': 'records', 'intent': 'inspect'})
        self.assertEqual(opened['withheld'][target], withheld)
        self.assertNotIn(target, active)
        context.member.guild_permissions.view_audit_log = False
        tool = discovery_tool(context, tools, {}, lambda: None)
        self.assertNotIn(target, tool.definition.parameters['properties']['group']['description'])
        opened = await tool.handler(context, {'group': 'records', 'intent': 'inspect'})
        self.assertNotIn(target, str(opened))

    async def test_outside_clan_war_log_opens_the_named_lookup_instead_of_paging_records(self):
        from elbow_helper.agent.engine.answer_inputs import read_handles
        from elbow_helper.agent.plan.checker import check_step

        context = _context()
        tools = build_agent_tools()
        active = {}
        tool = discovery_tool(context, tools, active, lambda: None)
        self.assertIn('read_clash', tool.definition.parameters['properties']['group']['description'])
        browse = {'group': 'records', 'intent': 'inspect'}
        page = await tool.handler(context, browse)
        self.assertEqual(read_handles({'group': page}, {'group': browse})['group']['source_next_arguments'],
                         {'group': 'records', 'intent': 'inspect', 'cursor': page['next_cursor']})
        active.clear()
        arguments = {'group': 'records', 'capabilities': ['read_clash'], 'intent': 'inspect'}
        self.assertTrue(check_step({'id': 'open', 'capability': 'open_capability_group',
                                    'arguments': arguments, 'depends_on': []},
                                   {'open_capability_group': tool}).ok)
        opened = await tool.handler(context, arguments)
        self.assertEqual(list(active), ['read_clash'])
        self.assertTrue(any(line.startswith('read_clash: ' + tools['read_clash'].definition.description + ' | read | ')
                            for line in opened['catalogue'].splitlines()))
        refused = await tool.handler(context, {'capabilities': ['read_clash', 'read_war_logs'], 'intent': 'inspect'})
        self.assertEqual((refused['cause'], refused['capabilities']), ('invalid_arguments', ['read_war_logs']))
        mixed = {'id': 'open', 'capability': 'open_capability_group', 'depends_on': [],
                 'arguments': {'group': 'transfers', 'capabilities': ['read_clash'], 'intent': 'inspect'}}
        self.assertTrue(check_step(mixed, {'open_capability_group': tool}).ok)
        active.clear()
        await tool.handler(context, mixed['arguments'])
        self.assertEqual(list(active), ['read_clash'])
        active.clear()
        inspected = await tool.handler(context, {'capabilities': ['send_direct_messages'], 'intent': 'inspect'})
        self.assertEqual(list(active), ['send_direct_messages'])
        self.assertIn('send_direct_messages:', inspected['catalogue'])
        self.assertFalse(context.state.proposed_changes)

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
        with patch('elbow_helper.agent.engine.catalogue.has_lookup_access', return_value=False):
            result = await flow.registry['open_capability_group'].handler(
                context, {'group': 'records', 'intent': 'inspect'})
        self.assertNotIn('query_bot_data', flow.registry)
        self.assertNotIn('query_bot_data', result['catalogue'])
        self.assertIn('read_member_cohort', flow.registry)


if __name__ == '__main__':
    unittest.main()
