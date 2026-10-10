"""Registry-wide properties of checked agent plans."""

import random
import unittest
import copy
from unittest.mock import patch

from elbow_helper.features.agent.plan import capability_list, check_plan, plan_definition
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.reports.tools import original_tool
from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentToolDefinition

from elbow_helper.features.agent.engine.capability_contract import contract_catalogue


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
    def test_catalogue_describes_nested_objects_and_object_arrays(self):
        tool = RegisteredAgentTool(
            AgentToolDefinition(
                "synthetic", "Inspect synthetic data.",
                {
                    "properties": {
                        "schedule": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string"},
                                "nested": {
                                    "type": "object",
                                    "properties": {"hour": {"type": "integer"}},
                                },
                            },
                        },
                        "sheets": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"sql": {"type": "string"}},
                            },
                        },
                    },
                },
            ),
            lambda *_: None,
        )
        catalogue = capability_list({"synthetic": tool})
        self.assertIn("schedule:{kind:string,nested:{hour:integer}}", catalogue)
        self.assertIn("sheets:[{sql:string}]", catalogue)

    def test_extra_parameter_keys_are_allowed_only_by_schema(self):
        from elbow_helper.features.agent.plan.checker import valid_arguments
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
        self.assertTrue(valid_arguments({"ids": [1, 2], "nested": {"anything": True}}, schema))
        self.assertFalse(valid_arguments(
            {"ids": [1, 2]}, {**schema, "additionalProperties": False},
        ))

    def test_unknown_step_keys_are_ignored_but_plan_keys_are_refused(self):
        tool = next(iter(self.registry.values()))
        plan = _plan_for(tool.definition.name, tool, tool.contract)
        plan["steps"][0]["reason"] = None
        self.assertTrue(check_plan(plan, self.registry).ok)
        plan["periods"] = []
        self.assertFalse(check_plan(plan, self.registry).ok)

    @classmethod
    def setUpClass(cls):
        cls.registry = build_agent_tools()

    def test_unexpected_checker_errors_are_logged_before_generic_feedback(self):
        tool = RegisteredAgentTool(AgentToolDefinition("synthetic", "Synthetic", {
            "type": "object", "properties": {}, "required": [],
        }), lambda *_: {})
        plan = {"goal": "Synthetic", "effort": "low", "output": "text",
                  "steps": [{
                    "id": "read", "capability": "synthetic", "arguments": {},
                    "reason": "Synthetic", "depends_on": [],
                }]}
        for error in (RuntimeError("Synthetic"), KeyError("Synthetic")):
            with (self.subTest(error=type(error).__name__),
                  patch("elbow_helper.features.agent.plan.checker.check_step", side_effect=error),
                  self.assertLogs("elbow_helper.features.agent.plan.checker", level="ERROR") as logs):
                result = check_plan(plan, {"synthetic": tool})
            self.assertFalse(result.ok)
            self.assertEqual(result.error, "Correct the plan fields and values.")
            self.assertTrue(any("Traceback" in line for line in logs.output))

    def test_reads_cannot_depend_on_unrun_changes(self):
        async def handler(context, values):
            return {}
        registry = {
            name: RegisteredAgentTool(AgentToolDefinition(name, "Synthetic", {
                "type": "object", "properties": {}, "required": [],
            }), handler, action_class=classification)
            for name, classification in (
                ("read", ActionClass.READ), ("change", ActionClass.CHANGE),
                ("irreversible", ActionClass.IRREVERSIBLE), ("output", ActionClass.OUTPUT),
            )
        }
        for change in ("change", "irreversible"):
            for indirect in (False, True):
                steps = [{"id": "changed", "capability": change, "arguments": {},
                          "reason": "Synthetic", "depends_on": []}]
                if indirect:
                    steps.append({"id": "output", "capability": "output", "arguments": {},
                                  "reason": "Synthetic", "depends_on": ["changed"]})
                steps.append({"id": "read", "capability": "read", "arguments": {},
                              "reason": "Synthetic",
                              "depends_on": ["output" if indirect else "changed"]})
                plan = {"goal": "Synthetic", "effort": "low", "output": "text",
                          "steps": steps}
                with self.subTest(change=change, indirect=indirect):
                    checked = check_plan(plan, registry)
                    self.assertFalse(checked.ok)
                    self.assertEqual(checked.step_id, "read")
                    self.assertEqual(checked.error,
                        "Plan reads before changes; a read can't use a change's result.")
                    steps[-1]["depends_on"] = []
                    self.assertTrue(check_plan(plan, registry).ok)

    def test_only_same_kind_irreversible_steps_share_a_preview(self):
        schema = {"type": "object", "properties": {}, "required": [],
                  "additionalProperties": False}
        registry = {
            name: RegisteredAgentTool(
                AgentToolDefinition(name, "Synthetic capability.", schema),
                lambda context, arguments: {}, action_class=action_class,
            ) for name, action_class in (
                ("read", ActionClass.READ),
                ("change", ActionClass.CHANGE),
                ("irreversible", ActionClass.IRREVERSIBLE),
                ("other_irreversible", ActionClass.IRREVERSIBLE),
            )
        }
        def plan(names):
            return {"goal": "Synthetic action", "effort": "low", "output": "text",
                      "steps": [{
                        "id": str(index), "capability": name, "arguments": {},
                        "reason": "Synthetic step", "depends_on": [],
                    } for index, name in enumerate(names)]}
        self.assertTrue(check_plan(plan(("read", "irreversible")), registry).ok)
        self.assertFalse(check_plan(plan(("irreversible", "change")), registry).ok)
        self.assertTrue(check_plan(plan(("irreversible", "irreversible")), registry).ok)
        self.assertFalse(check_plan(plan(("irreversible", "other_irreversible")), registry).ok)

    def test_catalogue_and_plan_tool_are_stable(self):
        self.assertEqual(capability_list(self.registry), capability_list(build_agent_tools()))
        self.assertEqual(plan_definition(self.registry), plan_definition(build_agent_tools()))
        self.assertEqual(len(capability_list(self.registry).splitlines()), len(self.registry))

    def test_valid_arguments_need_no_entities_or_periods(self):
        for name, tool in self.registry.items():
            plan = _plan_for(name, tool, CONTRACTS[name])
            plan.pop("entities", None)
            plan.pop("periods", None)
            with self.subTest(capability=name):
                checked = check_plan(plan, self.registry)
                self.assertTrue(checked.ok, checked.error)
        self.assertNotIn("entities", plan_definition(self.registry).parameters["required"])
        self.assertNotIn("periods", plan_definition(self.registry).parameters["required"])

    def test_saved_report_rejects_an_unsupported_filter_with_supported_names(self):
        name = "read_saved_report"
        kind = self.registry[name].definition.parameters["properties"]["report_kind"]["enum"][0]
        selected = original_tool(self.registry, name, {"report_kind": kind})
        contract = selected.contract
        plan = _plan_for(selected.definition.name, selected, contract)
        plan["steps"][0]["capability"] = name
        plan["steps"][0]["arguments"].update(report_kind=kind, unsupported_filter="value")
        check = check_plan(plan, self.registry)
        self.assertTrue(check.ok)
        self.assertIn("unsupported_filter", check.step_errors["first"])
        self.assertIn("Supported filters:", check.step_errors["first"])

    def test_malformed_plans_return_errors(self):
        generator = random.Random(7162)
        atoms = [None, True, False, 0, 1, "", "x", [], {}, [1, 2]]
        for _ in range(2000):
            raw = generator.choice(atoms)
            if generator.randrange(2):
                raw = {generator.choice(("goal", "effort", "output", "periods",
                                         "entities", "steps", "other")): raw}
            check = check_plan(raw, self.registry)
            self.assertFalse(check.ok)
            self.assertTrue(check.error)
        base = _plan_for(next(iter(self.registry)), next(iter(self.registry.values())),
                         CONTRACTS[next(iter(self.registry))])
        for _ in range(2000):
            raw = copy.deepcopy(base)
            raw[generator.choice(tuple(raw))] = generator.choice(atoms)
            check = check_plan(raw, self.registry)
            self.assertIsInstance(check.ok, bool)
            if not check.ok:
                self.assertTrue(check.error)

    def test_nested_malformed_steps_return_fixable_errors(self):
        generator = random.Random(7364)
        atoms = [None, True, 0, "", [], {}, [1, {}], {"step": [], "path": [{}]}]
        base = _plan_for(next(iter(self.registry)), next(iter(self.registry.values())),
                         CONTRACTS[next(iter(self.registry))])
        for _ in range(2000):
            plan = copy.deepcopy(base)
            step = plan["steps"][0]
            field = generator.choice(tuple(step))
            value = generator.choice(atoms)
            step[field] = value
            check = check_plan(plan, self.registry)
            reference = isinstance(value, dict) and set(value) == {"step", "path"}
            expected = value == base["steps"][0][field] or (
                field == "arguments" and not reference
            )
            self.assertEqual(check.ok, expected)
            if not check.ok:
                self.assertTrue(check.error)
