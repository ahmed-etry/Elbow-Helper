import unittest

from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.plan.checker import check_plan
from elbow_helper.agent.plan.executor import resolve_arguments


class MalformedReferenceTests(unittest.TestCase):
    def test_reference_strings_and_wrong_keys_name_the_reference_shape(self):
        registry = build_agent_tools()
        for capability, arguments, field in (
            ('read_clash', {'kind': 'player', 'tags': ['$ref:links.rows.*.player_tag']}, 'tags'),
            ('read_member_accounts', {'member_ids': {'path': ['members', '*', 'member_id']}}, 'member_ids'),
            ('read_hibernation', {'member_ids': [{'step': 'links', 'path': ['members', '*'],
                                               'field': 'member_id'}]}, 'member_ids'),
        ):
            with self.subTest(capability=capability):
                plan = {'goal': 'Read Birch accounts', 'effort': 'low', 'output': 'text', 'steps': [{
                    'id': 'read', 'capability': capability, 'arguments': arguments, 'depends_on': []}]}
                checked = check_plan(plan, registry)
                message = checked.error or checked.step_errors.get('read', '')
                self.assertIn('result reference', message)
                self.assertIn('"step"', message)
                self.assertIn('"path"', message)
                self.assertIn(field, message)
                with self.assertRaisesRegex(ValueError, 'result reference'):
                    resolve_arguments(arguments, {}, schema=registry[capability].definition.parameters)
        schema = registry['read_clash'].definition.parameters
        resolved = resolve_arguments({'kind': 'player', 'tags': {'step': 'links', 'path': ['tags']}},
                                     {'links': {'tags': ['#P0', '#Q0']}}, schema=schema)
        self.assertEqual(resolved['tags'], ['#P0', '#Q0'])
        # Ordinary message text and literal object fields keep their meaning.
        literal = {'text': '$ref:an example', 'row': {'path': 'woodland', 'field': 'birch'}}
        self.assertEqual(resolve_arguments(literal, {}, schema={'type': 'object', 'properties': {
            'text': {'type': 'string'}, 'row': {'type': 'object', 'additionalProperties': True}}}), literal)
