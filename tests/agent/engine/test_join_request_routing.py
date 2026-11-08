"""In-game entry settings and FAQ remain distinct from Discord transfer queues."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.reads import identity_reads
from elbow_helper.agent.reads import reference as reference_reads
from elbow_helper.agent.engine.catalogue import FAMILY_INSTRUCTION
from elbow_helper.agent.plan.format import answer_instructions
from elbow_helper.agent.knowledge.store import KnowledgeCatalog, KnowledgeSection
from elbow_helper.agent.models import EvidenceScope
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from agent.engine.test_agent_plan_flow import _context, _Model, _Session, _plan, _model_step
from agent.engine.test_planner_fix2_evidence import unpack


FAQ_TITLE = 'Why can I not request to rejoin after being rejected or kicked?'


class JoinRequestRoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_banned_notice_does_not_establish_a_clan_ban_list(self):
        for prompt in (FAMILY_INSTRUCTION, answer_instructions()):
            self.assertIn('do not invent', prompt.casefold())
            self.assertIn('leadership-managed', prompt)
            self.assertIn('ban-removal workflow', prompt)
            self.assertIn(FAQ_TITLE, prompt.replace('\n', ' '))
            self.assertIn('cleanup', prompt)

    async def test_blocked_in_game_request_reads_faq_and_live_settings_in_one_plan(self):
        section = KnowledgeSection(FAQ_TITLE,
            'A recent kick or rejection can impose a cooldown. Ask leadership for an invitation '
            'when trophies block entry, or ask them to clear the pending-request backlog.', file_name='faq.md')
        catalog = KnowledgeCatalog('synthetic', (section,)).prepare_prompts()
        context = replace(_context(), knowledge_store=SimpleNamespace(load=lambda: catalog))
        context.state.scope = EvidenceScope(91, frozenset(), frozenset())
        settings = {'type': 'inviteOnly', 'requiredTownhallLevel': 17,
                    'requiredTrophies': 4000, 'requiredBuilderBaseTrophies': 0}
        live = AsyncMock(return_value={'items': [{'tag': CLANS['BEH'].tag, 'status': 'ok',
            'data': {'name': 'Hellbow', 'members': 49, 'memberList': [], **settings}}]})
        roster = next(t for t in identity_reads.TOOLS if t.definition.name == 'read_clan_rosters')
        policy = next(t for t in reference_reads.TOOLS if t.definition.name == 'read_community_reference')
        registry = {t.definition.name: t for t in (roster, policy)}
        registry['hidden'] = replace(roster, default_visible=False)
        plan = {**_plan([
            {'id': 'policy', 'capability': 'read_community_reference',
             'arguments': {'titles': [FAQ_TITLE]}, 'depends_on': []},
            {'id': 'entry', 'capability': 'read_clan_rosters', 'arguments': {
                'clan_codes': ['BEH'], 'basis': 'current_in_game', 'view': 'count'}, 'depends_on': []},
        ]), 'finish_after_reads': True}
        initial = _Session([_model_step(plan)], [])
        final = _Session([AgentStep('BEH is invite-only. Ask a clan leader for an in-game invitation.',
                                   (), AgentUsage(1000, 20))], [])

        class Model(_Model):
            def create_agent_session(self, **kwargs):
                if self.request is not None:
                    self.writer = kwargs
                    return final
                return super().create_agent_session(**kwargs)

        model = Model(initial)
        with (patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry),
              patch.object(identity_reads, 'read_clash', live)):
            flow, _ = AgentService(model)._start_flow(
                question="I can't send a transfer request to BEH on any account. Are the clan settings broken?",
                local_context='', context=context, conversation_history='')
            answer = await flow.run()
        visible = unpack(json.loads(model.writer['prompt'].split('\nAvailable evidence:\n', 1)[1]))['results']
        entry = visible['entry']['clans'][0]['entry_settings']
        self.assertEqual({key: entry[key] for key in settings}, settings)
        self.assertEqual(entry['join_method'], 'request_approval')
        self.assertIn('can send join requests', entry['type_meaning'])
        self.assertEqual(visible['policy']['rows'][0]['body'], section.body)
        self.assertIn(FAQ_TITLE, model.request['system_prompt'])
        self.assertIn('command-specific evidence', model.request['system_prompt'])
        self.assertIn('cooldown', section.body)
        self.assertIn('backlog', section.body)
        self.assertNotIn('/transfer', answer)
        self.assertNotIn('season', answer)
        self.assertFalse(context.state.proposed_changes)
        self.assertFalse(model.writer['tools'])
        live.assert_awaited_once()
        self.assertEqual(live.await_args.args[1]['tags'], [CLANS['BEH'].tag])
        self.assertEqual((len(initial.calls), len(final.calls)), (1, 1))

    async def test_unavailable_or_missing_settings_stay_unknown_and_zero_requirements_survive(self):
        tool = next(t for t in identity_reads.TOOLS if t.definition.name == 'read_clan_rosters')
        for status, data in (('unavailable', {}), ('ok', {'type': 'closed'}),
                             ('ok', {'type': 'open', 'requiredTrophies': 0})):
            with self.subTest(status=status, data=data):
                live = {'items': [{'tag': CLANS['BEH'].tag, 'status': status, 'data': data}]}
                with patch.object(identity_reads, 'read_clash', AsyncMock(return_value=live)):
                    result = await tool.handler(_context(), {
                        'clan_codes': ['BEH'], 'basis': 'current_in_game', 'view': 'count'})
                visible = answer_evidence({'entry': result})['entry']['clans'][0]['entry_settings']
                self.assertEqual(visible['type'], data.get('type'))
                self.assertEqual(visible['requiredTrophies'], data.get('requiredTrophies'))
                self.assertIsNone(visible['requiredTownhallLevel'])


    async def test_all_entry_types_reach_the_writer_with_unambiguous_meanings(self):
        tool = next(t for t in identity_reads.TOOLS if t.definition.name == 'read_clan_rosters')
        for kind, method in (('open', 'direct_join'), ('inviteOnly', 'request_approval'),
                             ('closed', 'invitation_required'), ('futureType', None)):
            with self.subTest(kind=kind):
                live = {'items': [{'tag': CLANS['BEH'].tag, 'status': 'ok',
                                  'data': {'type': kind, 'requiredTrophies': 0}}]}
                with patch.object(identity_reads, 'read_clash', AsyncMock(return_value=live)):
                    result = await tool.handler(_context(), {
                        'clan_codes': ['BEH'], 'basis': 'current_in_game', 'view': 'count'})
                entry = answer_evidence({'entry': result})['entry']['clans'][0]['entry_settings']
                self.assertEqual(entry['join_method'], method)
                self.assertEqual(entry['requiredTrophies'], 0)
                self.assertEqual(entry['type_meaning'] is None, method is None)
        self.assertIsNone(identity_reads.entry_settings({'type': 'inviteOnly'}, 'unavailable')['join_method'])
