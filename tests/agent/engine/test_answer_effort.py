"""Composition and repairs use bounded effort independently of research plans."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.models import RegisteredAgentTool
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.plan.input_rules import RULES
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentToolDefinition, AgentUsage
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from tests.agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan, _step
from tests.features.cwl_record_stub import LeagueRecord, attack, league_war, member
from tests.temp_directory import TemporaryDirectory


class AnswerEffortTests(unittest.IsolatedAsyncioTestCase):
    async def answer(self, steps, question="Review #P0", payload=None, parameters=None):
        session = _Session(steps, [])
        registry = {"read_value": RegisteredAgentTool(AgentToolDefinition(
            "read_value", "Read synthetic evidence.", parameters or {"type": "object", "properties": {}}),
            AsyncMock(return_value=payload or {}))}
        with patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=registry):
            answer = await AgentService(_Model(session)).answer(
                question=question, local_context="", context=_context())
        return answer, [call[2] for call in session.calls]

    async def test_writer_has_its_own_effort_even_after_high_planning(self):
        answer, efforts = await self.answer([
            _model_step(_plan([_step("read")], effort="low")),
            AgentStep("Keep recruiting.", (), AgentUsage()),
        ])
        self.assertEqual(answer, "Keep recruiting.")
        self.assertEqual(efforts, [AgentReasoningEffort.HIGH, AgentReasoningEffort.LOW])

    async def test_citation_repair_does_not_inherit_maximum_planner_effort(self):
        answer, efforts = await self.answer([
            _model_step(_plan([_step("read")], effort="max")),
            AgentStep("Keep recruiting. [[message:999]]", (), AgentUsage()),
            AgentStep("Keep recruiting.", (), AgentUsage()),
        ])
        self.assertEqual(answer, "Keep recruiting.")
        self.assertEqual(efforts, [AgentReasoningEffort.HIGH, AgentReasoningEffort.LOW, AgentReasoningEffort.LOW])


    async def test_later_plan_cannot_raise_writer_effort(self):
        answer, efforts = await self.answer([
            _model_step(_plan([_step("first")], effort="max")),
            _model_step(_plan([_step("second")], effort="low")),
            AgentStep("Keep recruiting.", (), AgentUsage()),
        ])
        self.assertEqual(answer, "Keep recruiting.")
        self.assertEqual(efforts, [AgentReasoningEffort.HIGH, AgentReasoningEffort.LOW, AgentReasoningEffort.LOW])


    async def test_fixing_a_refused_lookup_keeps_planning_effort(self):
        parameters = {"type": "object", "properties": {"side": {"type": "string"}},
                      RULES: [{"needs": ["side"], "error": "Choose a side."}]}
        refused = _step("read")
        fixed = {**_step("read"), "arguments": {"side": "game"}}
        answer, efforts = await self.answer([
            _model_step(_plan([refused], effort="high")),
            _model_step(_plan([fixed], effort="low")),
            AgentStep("Keep recruiting.", (), AgentUsage()),
        ], parameters=parameters)
        self.assertEqual(answer, "Keep recruiting.")
        self.assertEqual(efforts[:2], [AgentReasoningEffort.HIGH, AgentReasoningEffort.HIGH])


class CwlPeriodFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.record = await LeagueRecord(temporary.name).initialize()
        self.queries = self.record.queries(clock=lambda: datetime(2026, 11, 8, tzinfo=timezone.utc))
        self.wars = {}
        for code, prefix in (('BEH', 'P'), ('BES', 'Q')):
            for month, finished in ((10, 7), (11, 3)):
                wars = [league_war('#' + prefix + ('Y' if month == 10 else 'L') + '0289PYL'[index], index + 1,
                    int(datetime(2026, month, 2 + index, tzinfo=timezone.utc).timestamp()),
                    [member('#P0', townhall=18, position=1)],
                    [attack(1, '#P0', '#Q0', townhall=18, position=1,
                            stars=3 if month == 10 else 2)],
                    state='warEnded' if index < finished else 'inWar' if index == finished else 'preparation',
                    team_size=15) for index in range(7)]
                await self.record.week(code, f'2026-{month}', wars,
                                       league='Champion League II', final=finished == 7)
                if month == 11:
                    self.wars[code] = ['CWL:' + war['tag'] for war in wars[:finished]]

    async def replay(self, code, history_limit, *, social=False):
        def scores(selection):
            return {**_step('scores', {'metric': 'ass', 'clan_codes': [code], **selection}),
                    'capability': 'read_cwl_results'}

        initial = [scores({'history_limit': history_limit})]
        if social:
            initial.append({**_step('league', {'metric': 'league', 'clan_codes': [code]}),
                            'capability': 'read_cwl_results'})
        steps = [
            *([AgentStep('', (AgentToolCall('expand', 'load_research_context', '{}'),),
                         AgentUsage())] if social else []),
            _model_step(_plan(initial)),
            _model_step(_plan([scores({'season': '2026-11'})])),
            AgentStep('Synthetic running-season ASS over three finished wars.', (), AgentUsage()),
        ]
        session = _Session(steps, [])
        session.replace_tools = lambda tools: None
        context = replace(_context(), cwl_queries=self.queries)
        context.source_message.created_at = datetime(2026, 11, 8, tzinfo=timezone.utc)
        production_registry = build_agent_tools()
        tool = production_registry['read_cwl_results']
        handler = AsyncMock(wraps=tool.handler)
        # Hidden specialist tools make production use the family catalogue.
        # Omitting them silently gives this replay the larger research budget.
        registry = {'read_cwl_results': replace(tool, handler=handler),
                    'query_bot_data': production_registry['query_bot_data']}
        leagues = AsyncMock(return_value=[{'clan_code': code, 'current_league': 'Synthetic league'}])
        with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry), \
                patch('elbow_helper.agent.reads.war_tools.current_leagues', leagues):
            answer = await AgentService(_Model(session, route='social' if social else 'research')).answer(
                question='Do as requested.' if social else f'ASS for {code} this month',
                local_context=f'Synthetic request: ASS for {code} this month.', context=context)
        self.assertEqual(answer, 'Synthetic running-season ASS over three finished wars.')
        self.assertEqual(len(session.calls), 4 if social else 3)
        self.assertTrue(session.calls[-2][1], 'The refused lookup must get a correction with tools.')
        feedback = json.loads(session.calls[-2][0][0].content)['results']
        self.assertEqual(feedback['scores']['cause'], 'invalid_arguments')
        self.assertIn('Use season (YYYY-MM)', feedback['scores']['error'])
        # The invalid history request is stopped at plan check, before its handler.
        self.assertEqual([call.args[1]['metric'] for call in handler.await_args_list],
                         ['league', 'ass'] if social else ['ass'])
        result = json.loads(session.calls[-1][0][0].content)['results']['scores']
        expected = await asyncio.to_thread(self.queries.results, metric='ass',
                                           clan_codes=[code], war_ids=self.wars[code])
        self.assertIn('rows', result, (result, expected))
        self.assertEqual(result['rows'], expected['rows'])
        self.assertEqual(result['completed_wars'], 3)
        self.assertEqual(set(result['resolved_war_ids']), set(self.wars[code]))
        self.assertEqual(tuple(result['seasons']), ('2026-11',))
        self.assertEqual(session.steps, [])
        if social:
            self.assertEqual(feedback['league']['rows'][0]['current_league'], 'Synthetic league')
            leagues.assert_awaited_once()

    async def test_a_history_count_is_corrected_to_all_finished_wars_in_the_running_season(self):
        await self.replay('BEH', 2)

    async def test_b_another_clan_gets_the_same_running_season_scope(self):
        await self.replay('BES', 2)

    async def test_c_social_followup_keeps_the_correction_and_the_independent_league_read(self):
        await self.replay('BEH', 3, social=True)
