"""Registry-wide properties of checked agent plans."""

import unittest

from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.engine.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentToolDefinition

from elbow_helper.agent.engine.capability_contract import contract_catalogue


REGISTRY = build_agent_tools()
CONTRACTS = contract_catalogue(REGISTRY)


def _sample(schema):
    if "anyOf" in schema and "type" not in schema:
        return _sample(schema["anyOf"][0])
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "integer":
        return schema.get("minimum", 1)
    if kind == "boolean":
        return False
    if kind == "string" and "pattern" in schema:
        return "2026-01"
    if kind == "string":
        return "#P0" if schema.get("minLength", 1) <= 3 <= schema.get("maxLength", 1024) else "x" * schema.get("minLength", 1)
    if kind == "array":
        values = [_sample(schema["items"]) for _ in range(schema.get("minItems", 0))]
        if schema.get("uniqueItems"):
            choices = schema["items"].get("enum")
            values = (list(choices[:len(values)]) if choices else
                      [value + index if type(value) is int else str(value) + str(index)
                       for index, value in enumerate(values)])
        return values
    if kind == "object":
        return {field: _sample(schema["properties"][field]) for field in schema.get("required", ())}
    raise AssertionError(kind)


def _plan_for(name, tool, contract, selected_field=None):
    schema = tool.definition.parameters
    return {"goal": "Synthetic request", "effort": "low", "output": "text", "steps": [{
        "id": "first", "capability": name, "arguments": {field: _sample(schema["properties"][field])
        for field in schema.get("required", ())}, "depends_on": []}]}


class PlanContractTests(unittest.TestCase):

    def test_shared_argument_check_names_schema_errors(self):
        from elbow_helper.agent.plan.checker import argument_issue, valid_arguments
        cases = [('read_cwl_results', {'season': ' '}, 'season'),
                 ('read_cwl_results', {'limit': 0}, 'limit'),
                 ('read_cwl_results', {'latest_completed': 5.0}, 'latest_completed'),
                 ('read_clash', {'kind': 'player'}, 'tags')]
        for name, arguments, field in cases:
            with self.subTest(name=name, arguments=arguments):
                issue = argument_issue(REGISTRY, name, arguments)
                self.assertIn(field, issue.error)
                self.assertIs(valid_arguments(arguments, REGISTRY[name].definition.parameters), False)
        for schema, arguments, field in (
            ({'type': 'object', 'properties': {'window': {'type': 'object',
                'properties': {'limit': {'type': 'integer'}}, 'required': ['limit']}},
             'required': ['window']}, {'window': {'limit': 'bad'}}, 'window'),
            ({'type': 'object', 'properties': {'mode': {'type': 'integer'}}, 'anyOf': [
                {'properties': {'mode': {'enum': [1]}}}, {'properties': {'mode': {'enum': [2]}}}]},
             {'mode': 3}, 'mode'),
        ):
            tool = RegisteredAgentTool(AgentToolDefinition('synthetic', 'Synthetic', schema), lambda *_: {})
            self.assertIn(field, argument_issue({'synthetic': tool}, 'synthetic', arguments).error)


    def test_extra_parameter_keys_are_allowed_only_by_schema(self):
        from elbow_helper.agent.plan.checker import valid_arguments
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
        self.assertTrue(valid_arguments({"ids": [1, 2], "nested": {"anything": True}}, schema))
        self.assertFalse(valid_arguments(
            {"ids": [1, 2]}, {**schema, "additionalProperties": False},
        ))


    @classmethod
    def setUpClass(cls):
        cls.registry = build_agent_tools()
