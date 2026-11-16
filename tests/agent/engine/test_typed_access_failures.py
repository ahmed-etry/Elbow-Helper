"""Access failures carry the handler's cause through planning and answering."""

from importlib import import_module
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.engine.evidence_delta import access_refused
from elbow_helper.agent.engine.tool_call import execute_tool
from elbow_helper.agent.discord_actions.management import guard
from elbow_helper.agent.features.support_tickets.reads import read_support_ticket_report
from elbow_helper.agent.features.support_tickets.report import SupportTicketReport
from elbow_helper.agent.plan.results import _flag, model_result
from tests.agent.engine.test_agent_plan_flow import _context


class TypedAccessFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_causes_distinguish_invalid_filters_and_lost_sources(self):
        context = _context()
        report = Mock(spec=SupportTicketReport, guild_id=context.guild.id)
        context.state.reports['tickets'] = report
        result = model_result(await read_support_ticket_report(
            context, {'report_id': 'tickets', 'owner_can_send': 'yes'}))
        self.assertEqual(result['error'], 'The owner send-permission filter must be true or false.')
        self.assertNotIn('access_denied', result['flags'].get('limits', ()))
        self.assertFalse(access_refused(result))
        self.assertEqual(answer_evidence({'tickets': result})['tickets']['failed_read']['reason'],
                         result['error'])

        # A retained source can disappear between creating a job/import and reading it.
        source = SimpleNamespace(guild=context.guild, channel=SimpleNamespace(id=100),
                                 attachments=[SimpleNamespace(id=42)])
        context = replace(context, attachment_sources=(source,), conversation_root_id=2,
                          research_jobs=SimpleNamespace(scope=Mock(return_value=SimpleNamespace(
                              source_channel_id=100, job_id='a' * 32))))
        cases = [
            ('research.tools', 'read_discord_research_job', {'job_id': 'a' * 32}, {}),
            ('research.tools', 'retain_discord_research_report', {'job_id': 'a' * 32}, {}),
            ('research.execution', 'advance_discord_research_job', 'a' * 32, {'lease_owner': 'test'}),
            ('research.batches', '_load_scoped_jobs', ['a' * 32], {'minimum': 1}),
            *[('files.attachment_tools', name, {'attachment_id': 42}, {}) for name in
              ('import_csv_attachment', 'import_xlsx_attachment', 'import_text_attachment')],
        ]
        for module_name, name, arguments, options in cases:
            with self.subTest(handler=name):
                module = import_module('elbow_helper.agent.' + module_name)
                with patch.object(module, 'accessible_message_channel', AsyncMock(return_value=None)):
                    raw = await getattr(module, name)(context, arguments, **options)
                if isinstance(raw, tuple):
                    raw = raw[1]
                self.assertEqual(raw.get('cause'), 'access_denied')
                result = model_result(raw)
                self.assertTrue(access_refused(result))
                self.assertEqual(answer_evidence({'source': result})['source']['status'], 'denied')
                # Wording and old cached flags cannot become authorization facts.
                self.assertNotEqual(_flag(result['error']), 'access_denied')
                without_cause = {key: value for key, value in result.items() if key != 'cause'}
                without_cause['flags'] = {'status': 'failed', 'limits': ['access_denied']}
                self.assertFalse(access_refused(without_cause))
                self.assertEqual(answer_evidence({'source': without_cause})['source']['status'], 'unavailable')

                async def handler(context, arguments):
                    context.state.scope_audit.private_candidates.add('100')
                    return result

                private = json.loads(await execute_tool(name=name, handler=handler, arguments={}, context=context))
                self.assertTrue(private['available_privately'])
                context.state.scope_audit.private_candidates.clear()

                async def untyped_handler(context, arguments):
                    context.state.scope_audit.private_candidates.add('100')
                    return without_cause

                untyped = json.loads(await execute_tool(name=name, handler=untyped_handler, arguments={}, context=context))
                self.assertNotIn('available_privately', untyped)
                context.state.scope_audit.private_candidates.clear()

        # Permission guards can raise a sentence containing no classifier keyword.
        context.member.guild_permissions = SimpleNamespace(manage_channels=False)

        async def guarded_action(context, arguments):
            guard(context, 'manage_channels')

        raised = json.loads(await execute_tool(name='create_discord_channel', handler=guarded_action,
                                               arguments={}, context=context))
        self.assertEqual(raised['cause'], 'access_denied')
        self.assertTrue(access_refused(model_result(raised)))
        self.assertEqual(answer_evidence({'channel': model_result(raised)})['channel']['status'], 'denied')
