"""Retain public owner context without inventing account purpose or access."""

import json
from types import SimpleNamespace
import unittest

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.roles import CO_APPLICANT_ROLE_ID, SLEEPING_CO_ROLE_ID
from elbow_helper.agent.safety.access import has_access_requirements
from elbow_helper.agent.reads.domain_facts import clan_profile
from elbow_helper.agent.reads.member_context import owner_context
from tests.agent.engine.test_agent_plan_flow import _context


class RecommendationContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_evicted_current_request_snapshot_remains_complete_and_authorized(self):
        from elbow_helper.agent.engine.cohort_evidence import cohort_writer_evidence
        from elbow_helper.agent.reads.snapshots import FamilyReport
        context = _context()
        rows = [{'player_tag': '#P0', 'townHallLevel': 12},
                {'player_tag': '#Q0', 'townHallLevel': 13}]
        report = FamilyReport('synthetic', context.guild.id, 'read_clan_rosters', json.dumps({
            'rows': rows, 'summary': {'requester_id': context.member.id}}))
        snapshots = {report.report_id: (report, frozenset({context.source_message.channel.id}), frozenset())}
        result = await cohort_writer_evidence(context, {'roster': report.page(limit=1)}, snapshots=snapshots)
        from tests.agent.engine.test_writer_delivery_evidence import unpack
        self.assertEqual(unpack(result['roster'])['rows'], rows)
        self.assertNotIn(report.report_id, context.state.reports)

        context.source_message.channel.permissions_for = lambda _: SimpleNamespace(
            view_channel=False, read_message_history=False)
        denied = await cohort_writer_evidence(context, {'roster': report.page(limit=1)}, snapshots=snapshots)
        self.assertEqual(denied['roster']['cause'], 'report_unavailable')


    async def test_activity_enrichment_supersedes_only_the_same_account_population(self):
        from elbow_helper.agent.engine.cohort_evidence import cohort_writer_evidence
        from elbow_helper.agent.reads.snapshots import FamilyReport
        context = _context()
        snapshots, evidence = {}, {}
        for identity, tag, activity in [('basic', '#P0', False), ('enriched', '#P0', True),
                                         ('other', '#Q0', False)]:
            summary = {'requester_id': context.member.id, 'basis': 'clan_rosters'}
            if activity:
                summary['activity_coverage'] = {'discord_activity_coverage': {}}
            report = FamilyReport(identity, context.guild.id, 'read_member_cohort', json.dumps({
                'rows': [{'member_id': 12, 'accounts': [{'player_tag': tag}]}], 'summary': summary}))
            snapshots[identity] = (report, frozenset({context.source_message.channel.id}), frozenset())
            evidence[identity] = report.page(limit=1)
        visible = await cohort_writer_evidence(context, evidence, snapshots=snapshots)
        self.assertEqual(set(visible), {'enriched', 'other'})


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
