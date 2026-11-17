"""Asked-for Discord IDs survive delivery, independently of how members were selected."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.agent.engine.service import AgentService
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from tests.agent.engine.test_agent_plan_flow import _context, _Model, _Session


class AskedMemberIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_asked_ids_survive_names_mentions_bulk_and_sorting(self):
        identifiers = [223456789012345678, 123456789012345678]
        members = {value: SimpleNamespace(id=value, display_name=f'Synthetic member {index}')
                   for index, value in enumerate(identifiers)}
        for mentioned in (False, True):
            for selected in (identifiers[:1], identifiers, sorted(identifiers)):
                with self.subTest(mentioned=mentioned, selected=selected):
                    context = _context()
                    members[context.member.id] = context.member
                    context.guild.get_member = members.get
                    context.guild.get_role = lambda _: None
                    context.source_message.mentions = [members[v] for v in selected] if mentioned else []
                    labels = [f'<@{v}>' if mentioned else members[v].display_name for v in selected]
                    question = 'Give the Discord IDs for ' + ', '.join(labels)
                    context.source_message.content = question
                    generated = '\n'.join(str(v) for v in selected)
                    session = _Session([AgentStep(generated, (), AgentUsage())], [])
                    with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value={}):
                        answer = await AgentService(_Model(session)).answer(
                            question=question, local_context='', context=context)
                    self.assertEqual(answer, generated)
