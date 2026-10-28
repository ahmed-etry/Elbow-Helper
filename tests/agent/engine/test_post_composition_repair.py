"""A late identity check must not turn completed work into a research failure."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.access import AgentAccessLost
from elbow_helper.agent.engine.budgets import ASSESSMENT_LIMITS
from elbow_helper.agent.engine.routing import RouteDecision
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage, TextGenerationError
from agent.engine import test_request_cost as cost


def decision(*, workbook=False):
    return json.dumps({'intent': 'Review synthetic accounts', 'intent_source': 'request',
        'discussion_message_ids': [], 'settled_choices': [], 'open_choices': [],
        'facts': 'Synthetic finding about <@101>.\n[[accounts:roster]]',
        'recommendation': '', 'question': '', 'account_lists': [
            {'id': 'roster', 'title': 'Synthetic group', 'columns': [
                {'heading': 'Synthetic account', 'fact': 'name'},
                {'heading': 'Synthetic judgement', 'value': 'estimate'}], 'accounts': [
                {'account': '#S0', 'values': {'estimate': '70%'}}]}],
        **({'account_lists_spreadsheet': 'Synthetic workbook'} if workbook else {})})


class PostCompositionRepairTests(unittest.IsolatedAsyncioTestCase):
    def flow(self, *, assessment=True, workbook=False, correction=None):
        steps = [AgentStep(correction, (), AgentUsage(1000, 100))] if correction else []
        flow, session, model, usage = cost.RequestCostTests().flow(steps)
        flow.assessment_requested = assessment
        flow.rounder.final_attempted = True
        flow.rounder.rounds = 4
        flow.answer_accounts = {'#S0': {'name': 'Synthetic account', 'town_hall': 10}}
        flow.context.guild.filesize_limit = 8 * 1024 * 1024
        # Synthetic replay of a completed large assessment's cumulative usage.
        flow.budget.request_limits = ASSESSMENT_LIMITS
        flow.budget.spent_input = 271_081
        flow.budget.spent_output = 56_325
        flow.budget.spent_rounds = 5
        flow.budget.next_input_estimate = 200_726
        actor = flow.context.member
        nickname = SimpleNamespace(id=101, display_name='Synthetic nickname')
        flow.context.guild.get_member = lambda identifier: nickname if identifier == 101 else actor if identifier == actor.id else None

        async def completed():
            return (decision(workbook=workbook) if assessment
                    else 'Synthetic finding about <@101>.')

        flow._run = AsyncMock(side_effect=completed)
        return flow, session, model, usage

    async def answer(self, flow, model, usage, *, question='Review the synthetic group'):
        def start(**options):
            flow.context = options['context']
            flow.context.state.family_catalogue = True
            return flow, usage

        service = AgentService(model)
        with patch.object(service, '_start_flow', side_effect=start), \
             patch('elbow_helper.agent.engine.service.judge_route',
                   AsyncMock(return_value=RouteDecision(social=False, usage=AgentUsage()))), \
             patch('elbow_helper.agent.engine.service.scoped_channel_directory',
                   AsyncMock(return_value='{}')), \
             patch('elbow_helper.agent.files.spreadsheet_tools.render_workbook_bytes',
                   AsyncMock(return_value=b'synthetic workbook')):
            return await service.answer(question=question, local_context='',
                                        context=flow.context)

    async def test_completed_assessment_survives_unaffordable_incidental_mention_repair(self):
        for workbook in (False, True):
            with self.subTest(workbook=workbook):
                flow, session, model, usage = self.flow(workbook=workbook)
                answer = await self.answer(flow, model, usage)
                self.assertTrue(answer.startswith('Synthetic finding about Synthetic nickname.'))
                self.assertNotIn('<@', answer)
                self.assertNotIn('.xlsx', answer)
                if not workbook:
                    self.assertIn('**Synthetic group (1)**\nSynthetic account — 70%', answer)
                self.assertTrue(flow.decision_composed)
                self.assertTrue(flow.rounder.budget_limited)
                self.assertEqual(session.calls, [])  # Refused before any provider call.
                self.assertEqual(flow.budget.spent_rounds, 5)
                self.assertEqual(len(flow.context.state.attachments), int(workbook))
                if workbook:
                    prepared, = flow.context.state.attachments
                    self.assertEqual(prepared.filename, 'synthetic-workbook.xlsx')
                    self.assertEqual(prepared.sheet_rows, (('Synthetic group', 1),))

    async def test_same_plain_reply_path_keeps_findings_without_an_unverified_ping(self):
        flow, session, model, usage = self.flow(assessment=False)
        self.assertEqual(await self.answer(flow, model, usage), 'Synthetic finding about Synthetic nickname.')
        self.assertFalse(flow.decision_composed)
        self.assertTrue(flow.rounder.budget_limited)
        self.assertEqual(session.calls, [])

    async def test_authorized_mention_keeps_finished_assessment_without_spending_a_repair(self):
        flow, session, model, usage = self.flow(workbook=True)
        flow.context.source_message.raw_mentions = [101]
        answer = await self.answer(flow, model, usage)
        self.assertEqual(answer, 'Synthetic finding about <@101>.')
        self.assertEqual(flow.context.operator_debug.status, 'completed')
        self.assertEqual(session.calls, [])
        self.assertEqual(len(flow.context.state.attachments), 1)

    async def test_other_unavailable_repairs_preserve_the_identity_guard_and_prepared_workbook(self):
        for error in (TextGenerationError('Synthetic provider failure'), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                flow, _, model, usage = self.flow(workbook=True)
                flow._advance_answer = AsyncMock(side_effect=error)
                self.assertEqual(await self.answer(flow, model, usage), 'Synthetic finding about Synthetic nickname.')
                self.assertEqual(len(flow.context.state.attachments), 1)
                flow._advance_answer.assert_awaited_once()

    async def test_unavailable_repair_preserves_addressed_prose_and_its_workbook(self):
        flow, session, model, usage = self.flow(workbook=True)
        flow._advance_answer = AsyncMock(side_effect=TimeoutError())
        answer = await self.answer(flow, model, usage,
                                  question='Send the synthetic findings to the named member')
        self.assertEqual(answer, 'Synthetic finding about Synthetic nickname.')
        self.assertEqual(len(flow.context.state.attachments), 1)
        self.assertEqual(session.calls, [])

    async def test_missing_guild_members_are_dropped_and_all_other_findings_survive(self):
        flow, _, model, usage = self.flow(workbook=True)
        actor = flow.context.member
        flow.context.guild.get_member = lambda identifier: actor if identifier == actor.id else None
        answer = await self.answer(flow, model, usage)
        self.assertEqual(answer, 'Synthetic finding about .')
        self.assertEqual(len(flow.context.state.attachments), 1)

    async def test_every_unverified_mention_is_neutralized_but_authorized_mentions_survive(self):
        flow, _, model, usage = self.flow(assessment=False)
        flow.context.source_message.raw_mentions = [303]
        flow._run = AsyncMock(return_value='Facts: <@101>, <@!101>, <@202>, <@303>.')
        nickname = flow.context.guild.get_member(101)
        nickname.display_name = '**Synthetic** @everyone <@404>'
        answer = await self.answer(flow, model, usage)
        self.assertEqual(answer.count('\\*\\*Synthetic\\*\\*'), 2)
        self.assertNotIn('<@101>', answer)
        self.assertNotIn('<@!101>', answer)
        self.assertNotIn('<@202>', answer)
        self.assertNotIn('@everyone', answer)
        self.assertNotIn('<@404>', answer)
        self.assertIn('<@303>', answer)

    async def test_source_access_loss_is_never_converted_to_an_identity_question(self):
        flow, _, model, usage = self.flow()
        flow._advance_answer = AsyncMock(side_effect=AgentAccessLost('Synthetic access loss'))
        with self.assertRaises(AgentAccessLost):
            await self.answer(flow, model, usage)

    async def test_affordable_repair_still_uses_the_models_corrected_reply(self):
        flow, session, model, usage = self.flow(correction='Which synthetic member?')
        flow.budget.spent_input = flow.budget.spent_output = 1000
        flow.budget.next_input_estimate = 1000
        self.assertEqual(await self.answer(flow, model, usage), 'Which synthetic member?')
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(flow.context.operator_debug.status, 'completed')


if __name__ == '__main__':
    unittest.main()
