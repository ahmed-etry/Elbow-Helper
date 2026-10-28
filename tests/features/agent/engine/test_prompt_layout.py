"""Offline request-prefix checks across every agent session factory."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.engine.assessment import select_decision_sources
from elbow_helper.features.agent.engine.routing import RouteDecision, judge_route
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.engine.recommended_actions import action_catalogue
from elbow_helper.features.agent.plan.format import (
    answer_instructions, output_forms, plan_definition, system_instructions,
)
from elbow_helper.features.agent.scheduled.watchers import evaluate
from elbow_helper.features.agent.prompts import LIMIT_ANSWER_INSTRUCTION
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage
from elbow_helper.infrastructure.ai.client import _FINAL_ANSWER_INSTRUCTION
from features.agent.engine.test_agent_plan_flow import _context


class RecordingModel:
    def __init__(self):
        self.requests = []

    def create_agent_session(self, **request):
        recorded = deepcopy(request)
        self.requests.append(recorded)
        tools = request['tools']
        selected = tools and tools[0].name == 'select_discussion_sources'
        step = AgentStep('{"holds":false,"alert":""}',
            (AgentToolCall('selection', 'select_discussion_sources', '{"channel_ids":[]}'),)
            if selected else (), AgentUsage(100, 10))
        async def advance(*args, **options):
            recorded['continuation_instruction'] = options.get('continuation_instruction', '')
            recorded['allow_tools'] = options.get('allow_tools', True)
            return step

        return SimpleNamespace(context_window_tokens=1_000_000,
            advance=AsyncMock(side_effect=advance), replace_tools=lambda _: None)


async def capture_requests(marker):
    """Use synthetic contexts and sessions; never construct a provider transport."""
    context = _context()
    context.member.id = 4101 if marker == 'ALPHA' else 4202
    context.member.display_name = marker + '_asker'
    context.member.roles.append(SimpleNamespace(id=8101 if marker == 'ALPHA' else 8202))
    context.source_message.id = 7101 if marker == 'ALPHA' else 7202
    context.source_message.created_at = datetime(2026, 1 if marker == 'ALPHA' else 2, 1,
                                                tzinfo=timezone.utc)
    context.source_message.channel.id = 9101 if marker == 'ALPHA' else 9202
    context.source_message.channel.name = marker + '_channel'
    context.state.channel_directory = marker + '_directory'
    context.state.reply_context = {'subject': marker + '_subject'}
    directory = json.dumps({str(context.source_message.channel.id): marker + '_channel'})
    model = RecordingModel()
    context = replace(context, bot=SimpleNamespace(agent_model=model))
    requests = {}

    def remember(name):
        requests[name] = model.requests[-1]

    await judge_route(model, question=marker + '_question', local_context=marker + '_nearby',
        conversation_history=marker + '_history', context=context)
    remember('routing')

    def start(route=None):
        flow, _ = AgentService(model)._start_flow(question=marker + '_question',
            local_context=marker + '_nearby', context=context,
            conversation_history=marker + '_history', route=route)
        return flow

    for name, route in (('planning', None), ('banter', RouteDecision(social=True)),
                         ('game_advice', RouteDecision(game_advice=True))):
        flow = start(route)
        await flow.rounder.advance(allow_tools=name != 'game_advice')
        remember(name)

    with patch('elbow_helper.features.agent.engine.flow.scoped_channel_directory',
               AsyncMock(return_value=directory)):
        for name, assessment, allow_more in (
            ('answer', False, False), ('review', False, True),
            ('assessment', True, False), ('assessment_review', True, True)):
            flow = start()
            flow.assessment_requested = assessment
            if not await flow._prepare_answer_session(allow_more=allow_more):
                raise AssertionError('Synthetic answer session did not fit')
            await flow._advance_answer(allow_tools=allow_more,
                continuation_instruction='' if allow_more else LIMIT_ANSWER_INSTRUCTION)
            remember(name)
            if name == 'answer':
                await flow._fresh_answer_recovery()
                await flow._advance_answer(allow_tools=False,
                    continuation_instruction=LIMIT_ANSWER_INSTRUCTION)
                remember('answer_recovery')

    flow = start()
    flow.assessment_requested = True
    await flow._repair_decision_format(marker + '_draft', ValueError(marker + '_error'))
    remember('decision_repair')

    flow = start()
    with patch('elbow_helper.features.agent.engine.assessment.scoped_channel_directory',
               AsyncMock(return_value=directory)):
        await select_decision_sources(flow)
    remember('discussion_selection')
    await evaluate(context, marker + '_condition', [{'value': marker + '_result'}])
    remember('watcher')
    return requests


def request_text(request):
    """Count characters, including schemas, without claiming provider token counts."""
    definitions = [{'name': tool.name, 'description': tool.description,
                    'parameters': tool.parameters} for tool in request['tools']]
    return (request['system_prompt'] + json.dumps(definitions, ensure_ascii=False) + request['prompt']
            + request.get('continuation_instruction', '')
            + (_FINAL_ANSWER_INSTRUCTION if request.get('allow_tools') is False else ''))


class PromptLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_call_keeps_request_values_after_identical_instructions_and_tools(self):
        first, second = await capture_requests('ALPHA'), await capture_requests('OMEGA')
        self.assertEqual(set(first), {'routing', 'planning', 'banter', 'game_advice',
            'answer', 'review', 'assessment', 'assessment_review', 'answer_recovery',
            'decision_repair', 'discussion_selection', 'watcher'})
        for name in first:
            with self.subTest(call=name):
                a, b = first[name], second[name]
                self.assertEqual(a['system_prompt'].encode(), b['system_prompt'].encode())
                self.assertEqual(a['tools'], b['tools'])
                self.assertNotEqual(a['prompt'], b['prompt'])
                fixed = a['prompt'].split('ALPHA', 1)[0]
                self.assertEqual(fixed.encode(), b['prompt'].split('OMEGA', 1)[0].encode())
                for request in (a, b):
                    for value in ('ALPHA', 'OMEGA', '4101', '4202', '7101', '7202',
                                  '9101', '9202', '8101', '8202',
                                  '2026-01-01', '2026-02-01'):
                        self.assertNotIn(value, request['system_prompt'])
                        self.assertNotIn(value, str(request['tools']))
                        self.assertNotIn(value, fixed)
        self.assertEqual(first['answer'], first['answer_recovery'])
        from elbow_helper.features.agent.engine.assessment import DECISION_ANSWER_INSTRUCTION
        self.assertIn(DECISION_ANSWER_INSTRUCTION, first['assessment']['system_prompt'])
        self.assertNotIn(DECISION_ANSWER_INSTRUCTION, first['assessment_review']['system_prompt'])
        self.assertEqual(first['decision_repair']['system_prompt'].count(DECISION_ANSWER_INSTRUCTION), 2)

    async def test_assessment_schema_is_fixed_in_fresh_sessions_and_kept_for_failed_factories(self):
        from elbow_helper.features.agent.engine.assessment import DECISION_ANSWER_INSTRUCTION

        for fresh in (False, True):
            with self.subTest(fresh=fresh):
                model = RecordingModel()
                flow, _ = AgentService(model)._start_flow(question='Assess synthetic facts',
                    local_context='', context=_context(), conversation_history='')
                flow.assessment_requested = True
                if not fresh:
                    flow.final_session_factory = lambda **_: None
                self.assertEqual(await flow._prepare_answer_session(), fresh)
                await flow._advance_answer(allow_tools=False, continuation_instruction='Write the answer.')
                session = flow.rounder.session
                continuation = session.advance.await_args.kwargs['continuation_instruction']
                if fresh:
                    self.assertIn(DECISION_ANSWER_INSTRUCTION, model.requests[-1]['system_prompt'])
                    self.assertNotIn(DECISION_ANSWER_INSTRUCTION, continuation)
                    await flow._fresh_answer_recovery()
                    await flow._advance_answer(allow_tools=False, continuation_instruction='Repair the answer.')
                    continuation = flow.rounder.session.advance.await_args.kwargs['continuation_instruction']
                    self.assertNotIn(DECISION_ANSWER_INSTRUCTION, continuation)
                else:
                    self.assertIn(DECISION_ANSWER_INSTRUCTION, continuation)

    async def test_fixed_request_guidance_precedes_history_and_metadata(self):
        requests = await capture_requests('ALPHA')
        for name in ('planning', 'banter'):
            with self.subTest(call=name):
                prompt = requests[name]['prompt']
                self.assertLess(prompt.index('A passing mention talks about you'),
                                prompt.index('<history_checkpoint>'))
                if name == 'planning':
                    self.assertLess(prompt.index('Canonical public command help'),
                                    prompt.index('<history_checkpoint>'))

    def test_retained_finding_guidance_precedes_data_only_when_findings_exist(self):
        from elbow_helper.features.agent.conversation.context import compile_context
        from elbow_helper.features.agent.conversation.findings import FINDINGS_INSTRUCTION
        from features.agent.conversation.test_agent_context import _turn

        context = _context()
        turn = _turn(1)
        context.state.authorized_history = (replace(turn,
            record=replace(turn.record, evidence=('Synthetic retained evidence',))),)
        for finding in (None, '{"fact":"Synthetic earlier finding"}'):
            with self.subTest(finding=finding):
                with patch('elbow_helper.features.agent.conversation.context.retained_finding',
                           return_value=finding):
                    compiled = compile_context(question='Continue', local_context='', context=context, tools=())
                if finding is None:
                    self.assertNotIn(FINDINGS_INSTRUCTION, compiled.prompt)
                else:
                    self.assertLess(compiled.prompt.index(FINDINGS_INSTRUCTION),
                                    compiled.prompt.index('<history_checkpoint>'))
                    self.assertIn('Synthetic earlier finding', compiled.answer_context)

    def test_fixed_extensions_precede_access_dependent_catalogues(self):
        registry = build_agent_tools()
        for instructions in (
            system_instructions(registry, extra_instructions=('Fixed extension.',)),
            answer_instructions(registry, extra_instructions=('Fixed extension.',)),
        ):
            self.assertLess(instructions.index('Fixed extension.'), instructions.index('<capabilities>'))
            self.assertEqual(instructions.count('Fixed extension.'), 1)

    def test_catalogues_and_output_forms_ignore_registration_order(self):
        registry = build_agent_tools()
        reversed_registry = dict(reversed(tuple(registry.items())))
        self.assertEqual(system_instructions(registry), system_instructions(reversed_registry))
        self.assertEqual(answer_instructions(registry), answer_instructions(reversed_registry))
        self.assertEqual(plan_definition(registry), plan_definition(reversed_registry))
        self.assertEqual(output_forms(registry), output_forms(reversed_registry))
        actions = action_catalogue(registry)
        self.assertTrue(json.loads(actions)['recommended_action_capabilities'])
        self.assertEqual(actions, action_catalogue(reversed_registry))

    def test_access_dependent_catalogues_follow_the_complete_fixed_rules(self):
        from elbow_helper.features.agent.conversation.reply_context import REPLY_CONTEXT_INSTRUCTION
        from elbow_helper.features.agent.engine.routing import SOCIAL_INSTRUCTION
        from elbow_helper.features.agent.engine.social_reply import SOCIAL_REPLY_INSTRUCTION
        from elbow_helper.features.agent.knowledge.answers import CHANNEL_FIRST_INSTRUCTION
        from elbow_helper.features.agent.prompts import RESEARCH_RULES
        from features.agent.engine.test_agent_plan_flow import _Model, _Session

        for route, rules in ((None, (RESEARCH_RULES, CHANNEL_FIRST_INSTRUCTION, REPLY_CONTEXT_INSTRUCTION)),
                             (RouteDecision(social=True), (SOCIAL_INSTRUCTION, SOCIAL_REPLY_INSTRUCTION,
                                 CHANNEL_FIRST_INSTRUCTION, REPLY_CONTEXT_INSTRUCTION))):
            with self.subTest(route=route):
                model = _Model(_Session([], []))
                AgentService(model)._start_flow(question='Synthetic request', local_context='',
                    context=_context(), conversation_history='', route=route)
                system = model.request['system_prompt']
                for rule in rules:
                    self.assertIn(rule, system[:system.index('<capabilities>')])

    def test_permission_mode_follows_shared_rules_without_losing_either_mode(self):
        from os.path import commonprefix
        from elbow_helper.features.agent.prompts import (
            ACTION_CAPABILITY_PARAGRAPH, ACTION_PLANNING_RULES, READ_ONLY_CAPABILITY_PARAGRAPH,
            STANDING_RULE_RULES,
        )

        allowed = system_instructions({}, extra_instructions=('Shared extension.',))
        readonly = system_instructions({}, actions_enabled=False,
                                       extra_instructions=('Shared extension.',))
        self.assertIn('Shared extension.', commonprefix((allowed, readonly)))
        self.assertIn(ACTION_CAPABILITY_PARAGRAPH, allowed)
        self.assertIn(ACTION_PLANNING_RULES, allowed)
        self.assertIn(STANDING_RULE_RULES, allowed)
        self.assertNotIn(READ_ONLY_CAPABILITY_PARAGRAPH, allowed)
        self.assertIn(READ_ONLY_CAPABILITY_PARAGRAPH, readonly)
        self.assertNotIn(ACTION_CAPABILITY_PARAGRAPH, readonly)
        self.assertNotIn(ACTION_PLANNING_RULES, readonly)

