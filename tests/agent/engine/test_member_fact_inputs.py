"""Synthetic reference boundaries; source prose is never rewritten by matching."""

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from elbow_helper.configuration.roles import LEAD
from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.engine.member_facts import read_topics
from elbow_helper.agent.families.bot_workflows import bot_workflows
from elbow_helper.agent.plan.format import answer_instructions
from elbow_helper.agent.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentToolDefinition


class MemberFactInputTests(unittest.TestCase):
    def setUp(self):
        self.guild = SimpleNamespace(roles=[SimpleNamespace(id=identity, name=f'Officer {index}')
            for index, identity in enumerate(sorted(LEAD))])

    def test_reference_labels_disappear_while_conditions_and_links_survive(self):
        body = 'Only elected officers with a silver badge may approve visitors.'
        link = 'https://discord.com/channels/1/2/3'
        raw = {'policy': {'family': 'read_community_reference',
            'selection': {'reference_kind': 'knowledge'}, 'rows': [{
                'section_id': 'PRIVATE_LOCATOR', 'file_name': 'PRIVATE_FILE',
                'title': 'PRIVATE_LABEL', 'visibility': 'lead', 'body': body, 'source': link}]}}
        original = deepcopy(raw)
        projected = answer_evidence(raw, guild=self.guild)
        self.assertEqual(projected['policy']['rows'], [{'body': body, 'source': link}])
        self.assertNotIn('PRIVATE_', json.dumps(projected))
        self.assertEqual(raw, original)

    def test_workflow_and_nested_reference_keep_facts_without_engineering_notes(self):
        raw = {'family': 'read_community_reference',
               'selection': {'reference_kind': 'bot_workflows'}, **bot_workflows()}
        projected = answer_evidence({'retained': {'workflow': raw}}, guild=self.guild)
        rows = projected['retained']['workflow']['rows']
        self.assertEqual(rows[0]['formula'], raw['rows'][0]['formula'])
        self.assertEqual(rows[1]['support']['operator_roles'], [['Officer 0', 'Officer 1']])
        self.assertFalse(rows[2]['clan_option_grants_approval'])
        self.assertEqual(rows[2]['command_clan_options'], raw['rows'][2]['supported_clan_codes'])
        self.assertNotIn('supported_clan_codes', rows[2])
        for row in rows:
            self.assertNotIn('sources', row)
            self.assertNotIn('placement_authority', row)
        self.assertNotIn('requester_lookup', rows[1])

    def test_denial_preserves_actual_roles_without_private_error_text(self):
        projected = answer_evidence({'restricted': {'required_access': ['lead'],
            'error': 'PRIVATE_ERROR', 'available_privately': True}}, guild=self.guild)
        self.assertEqual(projected['restricted'], {'status': 'denied',
            'reader_roles': [['Officer 0', 'Officer 1']], 'available_privately': True})

    def test_trusted_role_semantics_resolves_group_aliases_to_current_names(self):
        raw = {'roles': {'family': 'read_community_reference',
            'selection': {'reference_kind': 'roles'},
            'rows': [{'subject': 'Lead', 'holds': ['Lead Plus']} ]}}
        projected = answer_evidence(raw, guild=self.guild)
        self.assertEqual(projected['roles']['rows'], [{'subject': ['Officer 0', 'Officer 1'],
            'holds': [['Officer 0', 'Officer 1']]}])

    def test_member_records_are_preserved_even_with_reference_shaped_keys(self):
        raw = {'chat': {'messages': [{'title': 'Member title', 'source': 'Member text',
            'content': 'Keep every word and punctuation?!'}]}}
        self.assertEqual(answer_evidence(raw, guild=self.guild), raw)

    def test_configured_clan_flags_cannot_fill_unknown_operational_facts(self):
        raw = {'clans': {'family': 'read_community_reference',
            'selection': {'reference_kind': 'clans'}, 'rows': [{
                'clan_code': 'C', 'name': 'Cedar', 'configured_cwl_only': True,
                'configured_membership_basis': 'seasonal_cwl_cohort',
                'operational_cwl_only': None, 'membership_basis': None,
                'operational_policy_status': 'open_owner_question'}]}}
        projected = answer_evidence(raw, guild=self.guild)
        self.assertEqual(projected['clans']['rows'], [{'clan_code': 'C', 'name': 'Cedar',
            'operational_cwl_only': None, 'membership_basis': None}])

    def test_writer_catalogue_excludes_tool_editorial_but_keeps_signatures(self):
        registry = {'sample': RegisteredAgentTool(AgentToolDefinition('sample',
            'PRIVATE_TOOL_DESCRIPTION', {'type': 'object', 'properties': {'member_id': {'type': 'integer'}}},
            usage_note='PRIVATE_TOOL_NOTE'), lambda *_: None)}
        system = answer_instructions(registry)
        self.assertNotIn('PRIVATE_TOOL_', system)
        self.assertIn('member_id:int', system)
        self.assertEqual(read_topics({'reads': [{'capability': 'read_cwl_results',
            'summary': 'PRIVATE_TOOL_DESCRIPTION'}]}), {'topics': ['CWL results, leagues and ASS scores']})
