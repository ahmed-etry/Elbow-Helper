"""Every registered read reports mistakes, gaps and empty data the same way.

1. A planner mistake (bad, missing or conflicting arguments) is a top-level error with a
   planner cause, never rows or a nested gap.
2. A source with no data or no setup is an error with a reason, or rows that say so.
3. A result with rows says what it covered.
4. Inputs knowable before running are refused at plan check, in the same round.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import unittest

from elbow_helper.agent.actions.contracts import ActionClass
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.engine.tool_call import execute_tool
from elbow_helper.agent.plan.checker import check_step
from elbow_helper.agent.plan.input_rules import RULES
from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.cwl.config import CWL_CLAN_CODES
from tests.agent.engine.test_agent_plan_flow import _context
from tests.temp_directory import TemporaryDirectory

PLANNER_CAUSES = {'invalid_arguments', 'needs_input'}
COVERAGE_KEYS = {'coverage', 'coverage_note', 'history_coverage', 'missing_sources', 'missing',
                 'selection_complete', 'count_complete', 'search_complete', 'history_may_be_incomplete'}
START, END = '2026-09-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00'
NAIVE = '2026-09-01T00:00:00'
NON_CWL = next(code for code in CLANS if code not in CWL_CLAN_CODES)
RECENT = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()

# A complete request each read accepts at plan check; reads absent here take {} or
# generated values for their schema's required fields.
COMPLETE = {
    'read_war_attendance': {'war_type': 'REG', 'latest_completed': 1},
    'read_war_attacks': {'war_type': 'both', 'latest_completed': 1},
    'read_member_cohort': {'basis': 'joined_discord'},
    'read_ranked_activity': {'after': START, 'member_ids': [41]},
    'read_member_accounts': {'member_ids': [41]},
    'read_discord_access': {'member_ids': [41]},
    'read_discord_members': {'member_ids': [41]},
    'search_discord_messages': {'query': 'synthetic'},
    'read_membership_history': {'after': RECENT},
    'read_clash': {'kind': 'clan', 'tags': ['BEH']},
    'start_discord_research_job': {'channel_id': 91, 'query': 'synthetic'},
    'start_discord_research_batch': {'channel_ids': [91, 92], 'kind': 'search', 'query': 'synthetic'},
    'remember_task_instruction': {'label': 'Synthetic', 'quote': 'not in the request'},
}

# Planner mistakes the plan check refuses before any read runs.
MISTAKES = {
    'read_war_attendance': [
        {'war_type': 'REG'}, {'war_type': 'REG', 'season': '2026-09', 'latest_completed': 2},
        {'war_type': 'REG', 'before': END}, {'war_type': 'REG', 'season': 'September'},
        {'war_type': 'REG', 'after': NAIVE}],
    'read_war_attacks': [
        {'war_type': 'CWL'}, {'war_type': 'REG', 'season': '2026-09', 'latest_completed': 2},
        {'war_type': 'REG', 'before': END}, {'war_type': 'both', 'after': NAIVE},
        {'war_type': 'REG', 'season': '2026-09', 'account_tags': ['#P0'], 'member_ids': [41]}],
    'read_cwl_results': [
        {'metric': 'ass'}, {'metric': 'bonus', 'clan_codes': [NON_CWL]},
        {'metric': 'ass', 'clan_codes': [CWL_CLAN_CODES[0]], 'history_limit': 2},
        {'metric': 'bonus', 'clan_codes': [CWL_CLAN_CODES[0]], 'history_limit': 3},
        {'metric': 'ass', 'clan_codes': [CWL_CLAN_CODES[0]], 'season': '2026-09', 'latest_completed': 3},
        {'metric': 'attendance', 'season': '2026-09', 'history_limit': 2},
        {'metric': 'signup_pool', 'season': '2026-09'}, {'metric': 'mega_ass', 'season_keys': ['2026-09']},
        {'metric': 'league', 'latest_completed': 1}, {'before': END}],
    'read_member_accounts': [
        {'name': 'Synthetic'}, {'account_names': ['Synthetic']},
        {'name': 'Synthetic', 'member_ids': [41], 'look_in': 'game'},
        {'cohort_ref': 'synthetic', 'account_tags': ['#P0']}, {'member_ids': [41], 'before': END}],
    'read_member_cohort': [
        {'basis': 'selected'}, {'basis': 'selected', 'member_ids': [41], 'cohort_ref': 'synthetic'},
        {'basis': 'selected', 'member_ids': [41], 'role_ids': [1]},
        {'basis': 'joined_discord', 'member_ids': [41]}, {'basis': 'joined_discord', 'role_name': 'Synthetic'},
        {'basis': 'joined_discord', 'exclude_leadership': True}, {'basis': 'role_holders'},
        {'basis': 'role_holders', 'role_name': 'Synthetic', 'role_ids': [1]},
        {'basis': 'role_holders', 'role_ids': [1], 'clan_codes': ['BEH'], 'clan_role': 'member'},
        {'basis': 'accepted_recruitment', 'joined_after': START},
        {'basis': 'joined_discord', 'joined_after': NAIVE}],
    'read_conversation_evidence': [
        {'mode': 'search'}, {'mode': 'search', 'channel_id': 91, 'channel_ids': [92]},
        {'mode': 'message_window', 'channel_id': 91}, {'mode': 'channel_window'},
        {'mode': 'attachments', 'channel_id': 91}],
    'read_recruitment_pipeline': [
        {'stage': 'ticket_evidence'}, {'stage': 'ticket_log'}, {'stage': 'tickets', 'before': END},
        {'stage': 'tickets', 'after': NAIVE}],
    'read_discord_members': [{'hibernation_report_id': 'synthetic', 'member_ids': [41]},
                             {'joined_after': NAIVE}],
    'search_discord_messages': [{'channel_id': 91, 'channel_ids': [92]}],
    'read_ranked_activity': [{'after': START}, {'after': START, 'cohort_ref': 'synthetic', 'member_ids': [41]}],
    'read_clash': [{'kind': 'clan', 'tags': ['BEH'], 'offset': 2}],
    'start_discord_research_job': [{'channel_id': 91}],
    'start_discord_research_batch': [
        {'channel_ids': [91, 92], 'kind': 'search'}, {'channel_ids': [91, 92], 'kind': 'history'},
        {'channel_ids': [91, 92], 'kind': 'history', 'after': START, 'query': 'synthetic'}],
}

# Mistakes only the run can see (they depend on the request time); the handler must
# still return them as one top-level planner error.
RUNTIME_MISTAKES = {
    'read_membership_history': [{'after': RECENT, 'history_after': '2025-01-01T00:00:00+00:00'}],
    'remember_task_instruction': [{'label': 'Synthetic', 'quote': 'not in the request'}],
    'retire_task_instruction': [{'instruction_id': 'synthetic'}],
}

# Results that hold no period because they are configuration, not observations.
NO_PERIOD = {'read_clan_health_settings'}


def _sample(detail):
    if 'enum' in detail:
        return detail['enum'][0]
    kind = detail.get('type')
    if kind == 'integer':
        return max(1, detail.get('minimum', 1))
    if kind == 'array':
        return [_sample(item) for item in [detail.get('items', {})] * max(1, detail.get('minItems', 1))]
    if kind == 'boolean':
        return True
    return 'synthetic'


def complete_request(tool):
    schema = tool.definition.parameters
    return deepcopy(COMPLETE.get(tool.definition.name) or {
        field: _sample(schema['properties'][field]) for field in schema.get('required') or ()})


def generic_mistakes(tool):
    """Missing required inputs, unreadable dates and reversed windows for any read."""
    schema = tool.definition.parameters
    base = complete_request(tool)
    always = [rule for rule in schema.get(RULES, ()) if not {'when', 'with'} & set(rule)
              and ('needs' in rule or 'needs_all' in rule)]
    if schema.get('required') or always:
        yield {}
    dates = [field for field, detail in schema.get('properties', {}).items()
             if detail.get('format') == 'date-time']
    for field in dates:
        yield {**base, field: 'yesterday'}
        if field.endswith('after') and field[:-5] + 'before' in dates:
            yield {**base, field: END, field[:-5] + 'before': START}


def step(name, arguments):
    return {'id': 'contract', 'capability': name, 'arguments': deepcopy(arguments), 'depends_on': []}


class ReadContractTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = build_agent_tools()
        cls.reads = {name: tool for name, tool in cls.registry.items() if tool.action_class is ActionClass.READ}

    def setUp(self):
        # Reads that need live Discord objects fail closed here; their logs are noise.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        # Reads open their default stores relative to the working directory; keep that in a temporary folder.
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        previous = os.getcwd()
        os.chdir(temporary.name)
        self.addCleanup(os.chdir, previous)

    async def run_read(self, name, arguments):
        """The production path: the plan check, then the tool runner around the handler."""
        checked = check_step(step(name, arguments), self.registry)
        if not checked.ok:
            return 'plan_check', {'error': checked.error, 'cause': 'invalid_arguments'}
        raw = await execute_tool(name=name, handler=self.registry[name].handler,
                                 arguments=deepcopy(arguments), context=_context(), timeout_seconds=10)
        return 'run', json.loads(raw)


    def test_planner_mistakes_are_refused_at_plan_check(self):
        checked = 0
        for name, tool in self.reads.items():
            for arguments in [*generic_mistakes(tool), *MISTAKES.get(name, ())]:
                with self.subTest(read=name, arguments=arguments):
                    result = check_step(step(name, arguments), self.registry)
                    self.assertFalse(result.ok)
                    self.assertTrue(result.error)
                    checked += 1
        self.assertGreater(checked, 100)

    async def test_mistakes_seen_only_at_run_time_are_one_planner_error(self):
        for name, cases in RUNTIME_MISTAKES.items():
            for arguments in cases:
                with self.subTest(read=name, arguments=arguments):
                    stage, result = await self.run_read(name, arguments)
                    self.assertEqual(stage, 'run')
                    self.assertIn(result.get('cause'), PLANNER_CAUSES)
                    self.assertTrue(result['error'])
                    self.assertFalse(result.get('rows'))

    async def test_complete_requests_without_data_report_gaps_not_planner_mistakes(self):
        for name, tool in self.reads.items():
            if name in RUNTIME_MISTAKES:
                continue
            arguments = complete_request(tool)
            with self.subTest(read=name, arguments=arguments):
                stage, result = await self.run_read(name, arguments)
                self.assertEqual(stage, 'run', result)
                # Rule 4: anything the read refuses for missing input must be a plan check.
                self.assertNotIn(result.get('cause'), PLANNER_CAUSES, result)
                if 'error' in result:
                    self.assertTrue(result['error'])
                    self.assertFalse(result.get('rows'), result)
                elif name not in NO_PERIOD:
                    self.assertTrue(COVERAGE_KEYS & set(result) or any(key.startswith('missing_')
                                                                       for key in result), result)


class InputRuleTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_agent_tools()

    def check(self, name, arguments):
        return check_step(step(name, arguments), self.registry)

    def test_cwl_history_counts_are_only_refused_for_ass_and_bonus(self):
        for metric in ('ass', 'bonus'):
            with self.subTest(metric=metric):
                result = self.check('read_cwl_results', {
                    'metric': metric, 'clan_codes': ['BEH'], 'history_limit': 2})
                self.assertFalse(result.ok)
                self.assertEqual(result.error,
                    'ASS and bonus do not support history_limit. Use season (YYYY-MM) for one season, '
                    'including the running one, season_keys for several seasons, or war_ids, '
                    'latest_completed or after/before for part of a season.')
                for period in ({'season': '2026-11'}, {'season_keys': ['2026-10', '2026-11']},
                               {'war_ids': ['synthetic']}, {'latest_completed': 1},
                               {'after': START, 'before': END}):
                    self.assertTrue(self.check('read_cwl_results', {
                        'metric': metric, 'clan_codes': ['BEH'], **period}).ok)
        for metric in ('performance', 'mega_ass', 'attendance'):
            with self.subTest(metric=metric):
                self.assertTrue(self.check('read_cwl_results', {
                    'metric': metric, 'clan_codes': ['BEH'], 'history_limit': 2}).ok)

    def test_a_report_continuation_skips_selection_rules(self):
        self.assertTrue(self.check('read_war_attendance', {'report_id': 'synthetic'}).ok)
        self.assertTrue(self.check('read_member_cohort', {'report_id': 'synthetic', 'cursor': 'next'}).ok)

    def test_a_result_reference_counts_as_given_and_its_value_waits_for_the_run(self):
        reference = {'step': 'earlier', 'path': ['report_id']}
        planned = step('read_war_attendance', {'war_type': 'REG', 'latest_completed': 1, 'cohort_ref': reference})
        planned['depends_on'] = ['earlier']
        self.assertTrue(check_step(planned, self.registry).ok)
        planned = step('read_cwl_results', {'metric': 'ass', 'clan_codes': [reference]})
        planned['depends_on'] = ['earlier']
        self.assertTrue(check_step(planned, self.registry).ok)


if __name__ == '__main__':
    unittest.main()
