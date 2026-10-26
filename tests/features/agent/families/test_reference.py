"""Reference access and conversation bodies remain complete and source scoped."""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from elbow_helper.features.agent.families import reference_reads as reads
from elbow_helper.features.agent.knowledge.store import KnowledgeSection
from elbow_helper.features.agent.models import EvidenceScope
from elbow_helper.features.agent.engine.service import AgentService
from features.agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan
from elbow_helper.features.agent.research import history


class ReferenceFamilyTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_pages_start_with_later_replies_and_keep_backward_source_cursor(self):
        original = {'messages': [{'message_id': 20, 'content': 'Revision.'},
                                  {'message_id': 10, 'content': 'Proposal.'}],
                    'channel_id': 91, 'coverage': {'order': 'newest_first',
                        'next_cursor': 'opaque-older', 'reached_requested_start': False}}
        with patch.object(reads, 'read_discord_channel_history', AsyncMock(return_value=original)):
            result = await reads.conversation_evidence(_context(), {'mode': 'channel_window', 'channel_id': 91})
        self.assertEqual([row['message_id'] for row in result['rows']], [20, 10])
        self.assertEqual(result['coverage']['order'], 'newest_first')
        self.assertEqual(result['coverage']['source_order'], 'newest_first')
        self.assertEqual(result['source_next_cursor'], 'opaque-older')

    async def test_policy_scope_is_available_in_index_and_opened_reference(self):
        context = self.reference_context()
        catalog = context.knowledge_store.load()
        section = KnowledgeSection('Program entry', 'Requirements.', applicability='New programs only.')
        catalog.active_sections = (section,)
        catalog.visible_sections = lambda levels: catalog.active_sections
        index = await reads.community_reference(context, {})
        opened = await reads.community_reference(context, {'titles': [section.title]})
        for result in (index, opened):
            self.assertEqual(result['rows'][0]['applicability'], section.applicability)
        from elbow_helper.features.agent.engine.answer_inputs import answer_evidence
        facts = answer_evidence({'policy': {'family': 'read_community_reference', **opened}})
        self.assertEqual(facts['policy']['rows'][0]['applicability'], section.applicability)

    async def test_reply_chain_follows_only_structured_parent_ids(self):
        nodes = {2: {'message_id': 2, 'reply_to_message_id': 1, 'content': 'him'},
                 1: {'message_id': 1, 'reply_to_message_id': None, 'content': 'original'}}
        async def source(context, arguments, **kwargs):
            self.assertTrue(kwargs['complete_content'])
            return {'messages': [nodes[arguments['message_id']]]}
        with patch.object(reads, 'read_message_context', AsyncMock(side_effect=source)) as mocked:
            result = await reads.conversation_evidence(_context(), {'mode': 'reply_chain', 'message_id': 2})
        self.assertEqual([row['message_id'] for row in result['rows']], [1, 2])
        self.assertEqual(mocked.await_count, 2)
        self.assertEqual(result['missing_sources'], [])
        self.assertIsNone(result['remaining_message_id'])

    async def test_selected_section_id_opens_all_parts_of_only_that_heading(self):
        context = self.reference_context()
        catalog = context.knowledge_store.load()
        first = KnowledgeSection('Parts', 'first complete part', 'public', 'parts.md', part=1, parts=2)
        second = replace(first, body='second complete part', part=2)
        unrelated = KnowledgeSection('Other', 'unrequested content', 'public', 'parts.md')
        catalog.active_sections = (first, second, unrelated)
        catalog.visible_sections = lambda levels: catalog.active_sections
        from elbow_helper.features.agent.knowledge.tools import _section_id
        result = await reads.community_reference(context, {'section_ids': [_section_id(second)]})
        self.assertEqual([row['part'] for row in result['rows']], [1, 2])
        self.assertEqual([row['body'] for row in result['rows']], ['first complete part', 'second complete part'])

    async def test_attachment_text_preserves_complete_authorized_body_and_source(self):
        context = _context()
        attachment = SimpleNamespace(id=321, filename='Synthetic.txt', size=20000, content_type='text/plain')
        message = SimpleNamespace(id=123, guild=context.guild, channel=context.source_message.channel,
                                  attachments=[attachment], jump_url='authorized source')
        context = replace(context, attachment_sources=(message,))
        async def imported(context, arguments):
            context.state.reports['import'] = SimpleNamespace(report_id='import', text='evidence ' * 2000)
            return {'report_id': 'import'}
        supported = {'attachments': [{'attachment_id': 321, 'kind': 'text'}]}
        with patch.object(reads, 'list_supported_attachments', AsyncMock(return_value=supported)), \
                patch.object(reads, 'import_text_attachment', imported):
            result = await reads.conversation_evidence(context, {'mode': 'attachments',
                'projection': 'attachment_text', 'attachment_ids': [321]})
        self.assertEqual(result['rows'][0]['content'], 'evidence ' * 2000)
        self.assertEqual(result['rows'][0]['source'], 'authorized source')
        self.assertTrue(result['selection_complete'])
        self.assertNotIn('import', context.state.reports)

    def reference_context(self):
        context = _context()
        context.state.scope = EvidenceScope(91,frozenset(),frozenset())
        sections = (KnowledgeSection('Public','complete '+('body '*2000),'public','public.md'),
                    KnowledgeSection('Restricted','private body','lead','private.md'))
        catalog = SimpleNamespace(active_sections=sections,store_status='available',
            visible_sections=lambda levels:tuple(row for row in sections if row.visibility=='public' or row.visibility in levels))
        return replace(context, knowledge_store=SimpleNamespace(load=lambda:catalog))

    async def test_index_has_no_bodies_and_selected_public_body_is_complete(self):
        context = self.reference_context()
        index = await reads.community_reference(context,{})
        self.assertEqual([row['title'] for row in index['rows']],['Public'])
        self.assertNotIn('body',index['rows'][0])
        opened = await reads.community_reference(context,{'section_ids':[index['rows'][0]['section_id']]})
        self.assertEqual(len(opened['rows'][0]['body']),10009)
        self.assertIn(index['rows'][0]['section_id'],context.state.knowledge_sources)

    async def test_exact_excluded_title_does_not_disclose_body_or_heading(self):
        result = await reads.community_reference(self.reference_context(),{'titles':['Restricted']})
        self.assertEqual(result['rows'],[])
        self.assertNotIn('private body',str(result))
        self.assertNotIn('Restricted',str(result))

    async def test_message_window_keeps_full_text_structured_mentions_and_attachments(self):
        context = _context()
        channel = SimpleNamespace(id=91,name='Synthetic')
        message = SimpleNamespace(id=100,channel=channel,author=context.member,
            content='z'*5000,created_at=datetime.now(timezone.utc),reference=None,
            raw_mentions=[41],embeds=[],attachments=[SimpleNamespace(filename='Synthetic.txt',url='https://example.invalid/file',size=8)])
        async def messages(**kwargs):
            yield message
        channel.history=messages
        with patch.object(history,'accessible_message_channel',AsyncMock(return_value=channel)):
            result = await reads.conversation_evidence(context,{'mode':'message_window','channel_id':91,'message_id':100})
        self.assertEqual(result['rows'][0]['content'],'z'*5000)
        self.assertTrue(result['rows'][0]['content_complete'])
        self.assertEqual(result['rows'][0]['mentioned_member_ids'],[41])
        self.assertEqual(result['rows'][0]['attachments'][0]['filename'],'Synthetic.txt')

    async def test_typed_replay_delivers_only_exact_visible_answer_without_synthesis(self):
        context = _context()
        context.state.reply_context={'status':'resolved','message_id':100,
            'author':{'member_id':999},'conversation':{'requester_id':42,'request':'Old task',
                'delivered_answer':'Exact visible answer.','delivery_complete':True}}
        step={'id':'replay','capability':'read_conversation_evidence','arguments':{'mode':'replay'},'depends_on':[]}
        session=_Session([_model_step(_plan([step]))],[])
        flow,_=AgentService(_Model(session))._start_flow(question='Synthetic follow-up',local_context='',
                                                       context=context,conversation_history='')
        answer=await flow.run()
        self.assertEqual(answer,'Exact visible answer.')
        self.assertEqual(len(session.calls),1)

    async def test_recruitment_ticket_evidence_requests_complete_native_bodies(self):
        from elbow_helper.features.agent.families import workflow_reads
        from elbow_helper.configuration.channels import RECRUITMENT_TICKET_CATEGORY
        channel = SimpleNamespace(category_id=RECRUITMENT_TICKET_CATEGORY)
        source = AsyncMock(return_value={'messages':[{'content':'x'*5000}]})
        with patch.object(workflow_reads,'accessible_message_channel',AsyncMock(return_value=channel)), \
                patch.object(workflow_reads,'read_discord_channel_history',source):
            result = await workflow_reads.recruitment(_context(),
                {'stage':'ticket_evidence','ticket_channel_id':91,'after':'2026-10-01','member_ids':[41]})
        self.assertEqual(len(result['rows'][0]['content']),5000)
        self.assertTrue(source.call_args.kwargs['complete_content'])
        self.assertFalse(result['member_selection_complete'])

    async def test_private_pass_keeps_visible_causal_task_but_no_saved_tool_evidence(self):
        from elbow_helper.features.agent.knowledge.answers import private_answer_pass
        from features.agent.conversation.test_agent_context import _turn
        context = _context()
        context.source_message.reference = SimpleNamespace(message_id=1001,channel_id=91)
        turn = _turn(1,channel=91)
        turn = replace(turn,record=replace(turn.record,generated_answer='Hidden generated body',
                       evidence=('Saved tool payload',)))
        context.state.authorized_history = (turn,)
        async def answer(**kwargs):
            private = kwargs['context']
            self.assertEqual(len(private.history),1)
            self.assertEqual(private.history[0].record.question,'Question 1')
            self.assertEqual(private.history[0].record.generated_answer,'Answer 1')
            self.assertEqual(private.history[0].record.evidence,())
            self.assertEqual(private.history[0].record.report_ids,())
            self.assertFalse(private.state.reports)
            return 'Private answer'
        result = await private_answer_pass(SimpleNamespace(answer=answer),context,'Follow-up','')
        self.assertEqual(result[0],'Private answer')
