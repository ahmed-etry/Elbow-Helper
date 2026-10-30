"""Planner omissions are repairable; real ambiguity still needs the asker."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.families import access_reads, cohort_activity, identity_reads
from elbow_helper.agent.families import recruitment_history, reference_reads, war_reads, workflow_reads
from elbow_helper.agent.families.core import family_tool
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan


# Wording-preserving reclassification inventory. War reads require a period;
# cohort activity can read stored Discord counters without one.
RECLASSIFIED_ERRORS = (
    'Please specify after.',
    'Which members, accounts or exact name do you want?',
    'Which Discord role holders do you want?',
    'Choose both the attachment channel and message.',
    'Which message should I follow?',
    'Which message and channel should I read?',
    'Supply search words, a channel, an author, or a date range.',
    'Which channel should I read?',
    'Choose current guild roles.',
    'Choose one exact guild role name or ID.',  # Only zero matches; ambiguity stays needs_input.
    'Choose current guild members.',
    'Which roles or members should I compare?',
    'Which season, wars or date window do you want?',
    'Which clan should I score?',
    'Which recruitment ticket and date window should I read?',
    'Choose a dated recruitment history window.',
)


class RefusalRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_or_invalid_selectors_name_the_repair_fields(self):
        cases = (
            (identity_reads.member_accounts, {}, 'name'),
            (identity_reads.member_accounts, {'member_ids': [41], 'account_tags': ['#P0']}, 'member_ids'),
            (identity_reads.member_cohort, {'basis': 'role_holders'}, 'role_ids'),
            (reference_reads.conversation_evidence, {'mode': 'attachments', 'channel_id': 91}, 'message_id'),
            (reference_reads.conversation_evidence, {'mode': 'attachments', 'message_id': 71}, 'channel_id'),
            (reference_reads.conversation_evidence, {'mode': 'reply_chain'}, 'message_id'),
            (reference_reads.conversation_evidence, {'mode': 'message_window'}, 'message_id'),
            (reference_reads.conversation_evidence, {'mode': 'message_window', 'message_id': 71}, 'channel_id'),
            (reference_reads.conversation_evidence, {'mode': 'search'}, 'query'),
            (reference_reads.conversation_evidence, {'mode': 'author_mentions'}, 'mentions'),
            (reference_reads.conversation_evidence, {'mode': 'channel_window'}, 'channel_id'),
            (access_reads.discord_access, {}, 'member_ids'),
            (access_reads.discord_access, {'role_ids': [99]}, 'role_ids'),
            (access_reads.discord_access, {'role_names': ['Synthetic missing role']}, 'role_names'),
            (access_reads.discord_access, {'member_ids': [99]}, 'member_ids'),
            (war_reads.cwl_results, {'metric': 'ass'}, 'clan_codes'),
            (workflow_reads.recruitment, {'stage': 'ticket_evidence'}, 'ticket_channel_id'),
            (workflow_reads.recruitment, {'stage': 'ticket_evidence', 'ticket_channel_id': 91}, 'after'),
            (recruitment_history.recruitment_history, {'stage': 'acceptance_history'}, 'after'),
            (recruitment_history.recruitment_history,
             {'stage': 'ticket_log', 'after': 'invalid'}, 'after'),
        )
        errors = set()
        for reader, arguments, field in cases:
            with self.subTest(reader=reader.__name__, arguments=arguments):
                context = replace(_context(), cwl_queries=Mock())
                context.guild.get_role = lambda _: None
                context.guild.get_member = lambda _: None
                context.guild.roles = []
                with patch.object(workflow_reads, 'has_lookup_access', return_value=True):
                    result = await reader(context, arguments)
                self.assertEqual(result['cause'], 'invalid_arguments')
                errors.add(result['error'])
                self.assertIn(field, result.get('missing_fields', result.get('invalid_fields', [])))
                context.cwl_queries.assert_not_called()
                projected = answer_evidence({'read': result})['read']
                self.assertEqual(projected['status'], 'unavailable')
                self.assertNotIn('missing_fields', projected)
        self.assertEqual(errors, set(RECLASSIFIED_ERRORS) - {
            'Please specify after.', 'Which season, wars or date window do you want?'})

    async def test_missing_period_and_required_family_argument_are_repairable(self):
        context = replace(_context(), cwl_queries=Mock())
        results = [await war_reads.selected_wars(context, {}, 'both')]
        with patch.object(cohort_activity, 'selected_wars', AsyncMock()) as wars:
            activity = await cohort_activity.activity_projection(context, [], {})
        self.assertNotIn('error', activity)
        self.assertEqual(activity['war_source_status'], 'not_checked')
        self.assertEqual(activity['war_coverage']['reason'], 'no_period')
        wars.assert_not_awaited()
        reader = AsyncMock()
        tool = family_tool('read_synthetic', 'Read synthetic data.', {}, reader, required=('after',))
        results.append(await tool.handler(context, {}))
        for result in results:
            self.assertEqual(result['cause'], 'invalid_arguments')
            self.assertIn('after', result['missing_fields'])
            self.assertIn(result['error'], RECLASSIFIED_ERRORS)
        reader.assert_not_awaited()

    async def test_ambiguous_roles_remain_asker_input(self):
        context = _context()
        context.guild.roles = [SimpleNamespace(id=i, name='Synthetic role') for i in (1, 2)]
        result = await access_reads.discord_access(context, {'role_names': ['Synthetic role']})
        self.assertEqual(result['cause'], 'needs_input')
        self.assertEqual(answer_evidence({'read': result})['read']['status'], 'needs_input')

    async def test_missing_family_argument_reaches_planner_revision_then_succeeds(self):
        reader = AsyncMock(return_value={'rows': [{'value': 7}]})
        tool = family_tool('read_synthetic', 'Read synthetic data.',
                           {'after': {'type': 'string'}}, reader, required=('after',))
        def plan(arguments):
            return _plan([{'id': 'read', 'capability': 'read_synthetic',
                           'arguments': arguments, 'depends_on': []}])
        session = _Session([_model_step(plan({})),
                            _model_step(plan({'after': '2026-01-01T00:00:00Z'})),
                            AgentStep('Synthetic answer.', (), AgentUsage())], [])
        with patch('elbow_helper.agent.engine.service.build_agent_tools',
                   return_value={'read_synthetic': tool}):
            answer = await AgentService(_Model(session)).answer(
                question='Read since 2026-01-01.', local_context='', context=_context())
        self.assertEqual(answer, 'Synthetic answer.')
        self.assertEqual(len(session.calls), 3)
        reader.assert_awaited_once()
        self.assertEqual(reader.await_args.args[1]['after'], '2026-01-01T00:00:00Z')

