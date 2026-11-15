"""Unresolved reference diagnostics contain no source values."""
import unittest
import json
from elbow_helper.agent.plan.checker import check_plan, check_step
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.plan.executor import execute_plan, resolve_arguments


class UnresolvedReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_recorded_decimal_donor_index_checks_and_resolves_without_changing_mapping_keys(self):
        registry = build_agent_tools()
        reference = {'step': 'donors', 'path': ['rows', '0', 'member_id']}
        steps = [
            {'id': 'donors', 'capability': 'read_clan_rosters',
             'arguments': {'sort': 'donations', 'limit': 5}, 'depends_on': []},
            {'id': 'msgs', 'capability': 'read_conversation_evidence',
             'arguments': {'mode': 'search', 'query': 'transfer', 'author_id': reference, 'pages': 4},
             'depends_on': ['donors']},
            {'id': 'who', 'capability': 'read_member_accounts', 'arguments': {
                'member_ids': reference, 'include_profile': True}, 'depends_on': ['donors']},
        ]
        plan = {'goal': 'Find messages from the top donor', 'effort': 'high', 'output': 'text', 'steps': steps}
        checked = check_plan(plan, registry)
        self.assertTrue(checked.ok)
        calls = []
        async def run(step, arguments, earlier):
            self.assertTrue(check_step({**step, 'arguments': arguments}, registry, resolved=True).ok)
            calls.append((step['id'], arguments))
            return {'rows': [{'member_id': 1410538350984564749}]}
        await execute_plan(plan, run, argument_schema=lambda s: registry[s['capability']].definition.parameters)
        self.assertEqual(calls[1][1]['author_id'], 1410538350984564749)
        self.assertEqual(calls[2][1]['member_ids'], [1410538350984564749])
        # Only a reference with the declared item type gains a one-item list.
        detail = registry['read_member_accounts'].definition.parameters
        for value in ('1410538350984564749', True, None):
            resolved = resolve_arguments(steps[2]['arguments'], {'donors': {'rows': [{'member_id': value}]}}, schema=detail)
            self.assertIs(resolved['member_ids'], value)
        self.assertEqual(resolve_arguments({'member_ids': 1410538350984564749}, {}, schema=detail),
                         {'member_ids': 1410538350984564749})
        mapped = resolve_arguments(steps[1]['arguments'], {
            'donors': {'rows': {'0': {'member_id': 1410538350984564749}}}})
        self.assertEqual(mapped['author_id'], 1410538350984564749)
        reference['path'][1] = -1
        checked = check_plan(plan, registry)
        self.assertFalse(checked.ok)
        self.assertEqual((checked.step_id, checked.fields), ('msgs', ('author_id',)))
        detail = json.loads(checked.error.split(': ', 1)[1])
        self.assertEqual(detail['path'], reference['path'])
        self.assertEqual(detail['argument'], 'author_id')

    async def test_warning_names_step_and_path_without_result_values(self):
        for source in ({"roles": []}, {"roles": [{}]}, {"roles": None}, {}):
            calls = []
            async def run(step, arguments, earlier):
                calls.append(step["id"])
                return {**source, "secret": "synthetic-private-value"}
            plan = {"steps": [
                {"id": "source", "arguments": {}, "depends_on": []},
                {"id": "consumer", "arguments": {"nested": [{"step": "source", "path": ["roles", 0, "role_id"]}]},
                 "depends_on": ["source"]},
            ]}
            with self.subTest(source=source), self.assertLogs(
                "elbow_helper.agent.plan.executor", level="WARNING",
            ) as logs:
                results = await execute_plan(plan, run)
            self.assertEqual(results["consumer"], {"error": "A required earlier result is unavailable.",
                                                   "execution_status": "not_run"})
            self.assertEqual(calls, ["source"])
            self.assertEqual(len(logs.records), 1)
            self.assertIn("consumer", logs.output[0])
            self.assertIn("source", logs.output[0])
            self.assertIn("['roles', 0, 'role_id']", logs.output[0])
            self.assertNotIn("synthetic-private-value", logs.output[0])
