"""Only current scoped channel metadata reaches the final writer directory."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.engine.service import AgentService
from features.agent.engine.test_agent_plan_flow import _context
from features.agent.engine.test_planner_fix2_evidence import WriterModel


class ChannelWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_writer_receives_current_directory_and_omits_inaccessible_channels(self):
        context, model = _context(), WriterModel()
        source = context.source_message.channel
        source.name = 'current-room'
        context.guild.channels = (source, SimpleNamespace(id=92, name='base-designs'),
                                  SimpleNamespace(id=93, name='private-bases'))
        flow, _ = AgentService(model)._start_flow(question='Where can I share bases?',
            local_context='', context=context, conversation_history='')
        with patch('elbow_helper.features.agent.research.directory.can_access_message_channel',
                   AsyncMock(side_effect=lambda context, channel, **kw: channel.id != 93)):
            self.assertTrue(await flow._prepare_answer_session())
        evidence = json.loads(model.writer['prompt'].split('\nAvailable evidence:\n', 1)[1])['results']
        self.assertEqual(evidence['channel_directory']['channels'],
                         {'91': 'current-room', '92': 'base-designs'})
        self.assertIn('Mention or recommend only channels present there', model.writer['system_prompt'])
        self.assertNotIn('private-bases', model.writer['prompt'])
        self.assertFalse(context.state.proposed_changes)

    async def test_empty_directory_does_not_copy_stale_planning_channels(self):
        context, model = _context(), WriterModel()
        context.state.channel_directory = '{"93":"stale-bases"}'
        flow, _ = AgentService(model)._start_flow(question='Where can I share bases?',
            local_context='', context=context, conversation_history='')
        with patch('elbow_helper.features.agent.research.directory.can_access_message_channel',
                   AsyncMock(return_value=False)):
            self.assertTrue(await flow._prepare_answer_session())
        evidence = json.loads(model.writer['prompt'].split('\nAvailable evidence:\n', 1)[1])['results']
        self.assertNotIn('channel_directory', evidence)
        # The final evidence contains no stale locator from the planning index.
        self.assertNotIn('stale-bases', model.writer['prompt'])
