"""GAPS-FIX-2 regression: ExistingMemberLinkingTests."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.families.reference_reads import community_reference
from elbow_helper.agent.knowledge.store import KnowledgeCatalog, KnowledgeSection
from elbow_helper.agent.models import EvidenceScope
from agent.engine.test_agent_plan_flow import _context


class ExistingMemberLinkingTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_account_policy_supplies_existing_member_route_to_writer(self):
        context = _context()
        context.state.scope = EvidenceScope(91, frozenset(), frozenset())
        section = KnowledgeSection('Adding a village and leaving an unused account linked',
            'An unused account may remain linked.', file_name='faq.md')
        catalog = KnowledgeCatalog('synthetic', (section,)).prepare_prompts()
        context = replace(context, knowledge_store=SimpleNamespace(load=lambda: catalog))
        result = await community_reference(context, {'titles': [section.title]})
        visible = answer_evidence({'policy': {**result, 'family': 'read_community_reference',
            'selection': {'reference_kind': 'knowledge'}}}, guild=context.guild)['policy']
        workflow, = visible['procedures']['rows']
        self.assertEqual(workflow['account_linking']['existing_member_route'], '/profile')
        self.assertFalse(workflow['account_linking']['new_application_required_for_existing_member'])
        self.assertEqual(visible['rows'][0]['body'], section.body)
