"""Retain public owner context without inventing account purpose or access."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.roles import (
    LEAD, CO_APPLICANT_ROLE_ID, SLEEPING_CO_ROLE_ID,
)
from elbow_helper.features.agent.access import has_access_requirements
from elbow_helper.features.agent.families import identity_reads
from elbow_helper.features.agent.families.domain_facts import clan_profile
from elbow_helper.features.agent.families.member_context import owner_context
from elbow_helper.features.agent.families.model_view import family_model_view
from elbow_helper.features.agent.engine.answer_inputs import answer_evidence
from features.agent.engine.test_agent_plan_flow import _context


class RecommendationContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_evicted_current_request_snapshot_remains_complete_and_authorized(self):
        from elbow_helper.features.agent.engine.cohort_evidence import cohort_writer_evidence
        from elbow_helper.features.agent.families.core import FamilyReport
        context = _context()
        rows = [{'player_tag': '#P0', 'townHallLevel': 12},
                {'player_tag': '#Q0', 'townHallLevel': 13}]
        report = FamilyReport('synthetic', context.guild.id, 'read_clan_rosters', json.dumps({
            'rows': rows, 'summary': {'requester_id': context.member.id}}))
        snapshots = {report.report_id: (report, frozenset({context.source_message.channel.id}), frozenset())}
        result = await cohort_writer_evidence(context, {'roster': report.page(limit=1)}, snapshots=snapshots)
        from features.agent.engine.test_planner_fix2_evidence import unpack
        self.assertEqual(unpack(result['roster'])['rows'], rows)
        self.assertNotIn(report.report_id, context.state.reports)

        context.source_message.channel.permissions_for = lambda _: SimpleNamespace(
            view_channel=False, read_message_history=False)
        denied = await cohort_writer_evidence(context, {'roster': report.page(limit=1)}, snapshots=snapshots)
        self.assertEqual(denied['roster']['cause'], 'report_unavailable')

    async def test_roster_owner_leadership_reaches_writer_without_guessing_account_purpose(self):
        context = _context()
        context.member.roles = [SimpleNamespace(id=next(iter(LEAD)), name='Synthetic captain')]
        code = next(iter(CLANS))
        context = replace(context, account_links=SimpleNamespace(get_links_by_tags=lambda tags: {
            '#P0': {'discord_user_id': context.member.id}}))
        source = {'items': [{'tag': CLANS[code].tag, 'status': 'ok', 'data': {
            'members': 2, 'memberList': [
                {'tag': '#P0', 'name': 'Synthetic account', 'townHallLevel': 12},
                {'tag': '#Q0', 'name': 'Unlinked account', 'townHallLevel': 13}]}}]}
        with patch.object(identity_reads, 'read_clash', AsyncMock(return_value=source)):
            result = await identity_reads.clan_rosters(context, {'clan_codes': [code]})
        visible = answer_evidence({'roster': family_model_view({'family': 'read_clan_rosters', **result})})['roster']
        linked = next(row for row in visible['rows'] if row['player_tag'] == '#P0')
        unlinked = next(row for row in visible['rows'] if row['player_tag'] == '#Q0')
        self.assertEqual(linked['member_id'], context.member.id)
        self.assertEqual(linked['leadership']['status'], 'active')
        self.assertIsNone(linked['account_purpose'])
        self.assertEqual(unlinked['leadership']['status'], 'unknown')
        self.assertIsNone(unlinked['member_id'])
        summary = visible['clans'][0]
        self.assertEqual(summary['ownership']['distinct_linked_owners'], 1)
        self.assertEqual(summary['ownership']['linked_accounts'], 1)
        self.assertEqual(summary['ownership']['unknown_owner_accounts'], 1)

    async def test_activity_enrichment_supersedes_only_the_same_account_population(self):
        from elbow_helper.features.agent.engine.cohort_evidence import cohort_writer_evidence
        from elbow_helper.features.agent.families.core import FamilyReport
        context = _context()
        snapshots, evidence = {}, {}
        for identity, tag, activity in [('basic', '#P0', False), ('enriched', '#P0', True),
                                         ('other', '#Q0', False)]:
            summary = {'requester_id': context.member.id, 'basis': 'clan_rosters'}
            if activity:
                summary['activity_coverage'] = {'chat_coverage': {}}
            report = FamilyReport(identity, context.guild.id, 'read_member_cohort', json.dumps({
                'rows': [{'member_id': 12, 'accounts': [{'player_tag': tag}]}], 'summary': summary}))
            snapshots[identity] = (report, frozenset({context.source_message.channel.id}), frozenset())
            evidence[identity] = report.page(limit=1)
        visible = await cohort_writer_evidence(context, evidence, snapshots=snapshots)
        self.assertEqual(set(visible), {'enriched', 'other'})

    def test_activity_projection_keeps_leadership_and_account_progression(self):
        row = {'member_id': 12, 'member_name': 'Synthetic member',
               'home_clan_basis': 'Discord role assignment, not a main account location',
               'leadership': {'status': 'active', 'roles': []},
               'accounts': [{'player_tag': '#P0', 'townHallLevel': 12, 'role': 'coLeader',
                             'account_purpose': None}],
               'war_status': 'joined_without_attacking', 'wars_observed': 1}
        visible = family_model_view({'family': 'read_member_cohort', 'basis': 'clan_rosters',
            'activity_coverage': {'chat_coverage': {}}, 'rows': [row]})['rows'][0]
        self.assertEqual(visible['leadership']['status'], 'active')
        self.assertEqual(visible['accounts'][0]['townHallLevel'], 12)
        self.assertIn('account_purpose', visible['accounts'][0])
        self.assertIsNone(visible['accounts'][0]['account_purpose'])
        self.assertEqual(visible['wars_observed'], 1)
        self.assertEqual(visible['home_clan_basis'], row['home_clan_basis'])

    def test_sleeping_leadership_context_does_not_grant_workflow_access(self):
        member = SimpleNamespace(id=12, display_name='Synthetic sleeping captain',
                                 roles=[SimpleNamespace(id=SLEEPING_CO_ROLE_ID, name='Sleeping Co')])
        self.assertEqual(owner_context(member, 12)['leadership']['status'], 'sleeping')
        guild = SimpleNamespace(get_member=lambda _: member)
        self.assertFalse(has_access_requirements(guild, 12, {'recruitment'}))



    def test_configured_clan_is_not_proof_of_operational_status(self):
        for code in CLANS:
            self.assertIsNone(clan_profile(code)['operational_status'])

    def test_leadership_applicant_is_not_an_active_leader(self):
        member = SimpleNamespace(id=12, display_name='Synthetic applicant',
            roles=[SimpleNamespace(id=CO_APPLICANT_ROLE_ID, name='Co-Leader Applicant')])
        self.assertEqual(owner_context(member, 12)['leadership']['status'], 'applicant')

    def test_assessment_rules_reach_planner_and_isolated_writer_once(self):
        from elbow_helper.features.agent.prompts import ASSESSMENT_RULES
        from elbow_helper.features.agent.plan.format import system_instructions, answer_instructions
        for instructions in (system_instructions({}), answer_instructions()):
            self.assertEqual(instructions.count(ASSESSMENT_RULES), 1)
