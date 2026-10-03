"""Registry-wide properties of checked agent plans."""

from __future__ import annotations

import random
import unittest
import copy
import string
from re import _parser, _constants
from datetime import datetime, timezone

from elbow_helper.features.agent.plan import capability_list, check_plan, plan_definition
from elbow_helper.features.agent.plan.checker import (
    check_step, entity_kind, parse_periods, source_check, time_check,
)
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.reports.tools import original_tool
from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentToolDefinition

from elbow_helper.features.agent.engine.capability_contract import contract_catalogue
from elbow_helper.features.agent.reports.tools import saved_report_contracts


REGISTRY = build_agent_tools()
CONTRACTS = contract_catalogue(REGISTRY)
SAVED_REPORT_CONTRACTS = saved_report_contracts(REGISTRY)


def _sample(schema):
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "integer":
        return schema.get("minimum", 1)
    if kind == "boolean":
        return False
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


def _matching_value(pattern):
    def build(nodes):
        result = ""
        for kind, value in nodes:
            if kind is _constants.LITERAL:
                result += chr(value)
            elif kind is _constants.IN:
                first, item = value[0]
                result += chr(item[0] if first is _constants.RANGE else item) if first in (_constants.RANGE, _constants.LITERAL) else "0"
            elif kind in (_constants.MAX_REPEAT, _constants.MIN_REPEAT):
                result += build(value[2]) * value[0]
            elif kind is _constants.SUBPATTERN:
                result += build(value[-1])
            elif kind is _constants.BRANCH:
                result += build(value[1][0])
            elif kind is _constants.CATEGORY:
                result += "0"
            elif kind is not _constants.AT:
                raise AssertionError(kind)
        return result
    return build(_parser.parse(pattern, 0))


def _plan_for(name, tool, contract, selected_field=None):
    schema = tool.definition.parameters
    arguments = {field: _sample(schema["properties"][field]) for field in schema.get("required", ())}
    if selected_field:
        arguments[selected_field] = _sample(schema["properties"][selected_field])
    if contract.scope_variants:
        variants = dict(contract.scope_variants)
        chosen = next((key for key, selectors in variants.items() if selected_field in selectors),
                      next(iter(variants)))
        arguments[contract.scope_field] = chosen
        for field in variants[chosen]:
            arguments[field] = _sample(schema["properties"][field])
    for field, pattern in contract.value_patterns:
        if field in arguments:
            arguments[field] = _matching_value(pattern)
    periods = []
    if contract.time_window and (any(field in arguments for field in contract.time_window[:2])
                                or selected_field in contract.bounded_fields
                                or selected_field in contract.time_window[:2]
                                or not contract.latest_fields):
        lower, upper, encoding = contract.time_window
        arguments[lower], arguments[upper] = ((100, 200) if encoding == "unix_seconds" else
                                            ("2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"))
        periods.append({"kind": "utc_range", "start": _period_start(encoding).isoformat(),
                        "end": _period_end(encoding).isoformat()})
    for field in contract.time_fields:
        if field in arguments and field not in contract.latest_fields and field not in contract.bounded_fields:
            if not contract.time_window or field not in contract.time_window[:2]:
                periods.append({"kind": "key", "field": field, "value": arguments[field]})
    entities = [{"kind": entity_kind(kind), "value": item}
                for field, kind in contract.entity_fields if field in arguments
                for item in (arguments[field] if isinstance(arguments[field], list) else [arguments[field]])]
    return {"goal": "Read synthetic values", "effort": "low", "output": "text",
            "periods": periods, "entities": entities, "steps": [{
                "id": "step", "capability": name, "arguments": arguments,
                "reason": "Read selected values", "depends_on": [],
            }]}


def _period_start(encoding):
    return (datetime.fromtimestamp(60, timezone.utc) if encoding == "unix_seconds"
            else datetime(2026, 1, 1, tzinfo=timezone.utc))


def _period_end(encoding):
    return (datetime.fromtimestamp(300, timezone.utc) if encoding == "unix_seconds"
            else datetime(2026, 1, 4, tzinfo=timezone.utc))


class PlanContractTests(unittest.TestCase):
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
                        "periods": [], "entities": [], "steps": steps}
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
                    "periods": [], "entities": [], "steps": [{
                        "id": str(index), "capability": name, "arguments": {},
                        "reason": "Synthetic step", "depends_on": [],
                    } for index, name in enumerate(names)]}
        self.assertTrue(check_plan(plan(("read", "irreversible")), registry).ok)
        self.assertFalse(check_plan(plan(("irreversible", "change")), registry).ok)
        self.assertTrue(check_plan(plan(("irreversible", "irreversible")), registry).ok)
        self.assertFalse(check_plan(plan(("irreversible", "other_irreversible")), registry).ok)

    @classmethod
    def setUpClass(cls):
        cls.registry = build_agent_tools()

    def test_catalogue_and_plan_tool_are_stable(self):
        self.assertEqual(capability_list(self.registry), capability_list(build_agent_tools()))
        self.assertEqual(plan_definition(self.registry), plan_definition(build_agent_tools()))
        self.assertEqual(len(capability_list(self.registry).splitlines()), len(self.registry))

    def test_every_time_field_is_bounded_by_the_declared_period(self):
        checked = 0
        for name, tool in self.registry.items():
            contract = CONTRACTS[name]
            for field in contract.time_fields:
                with self.subTest(capability=name, field=field):
                    schema = tool.definition.parameters["properties"][field]
                    if contract.time_window and field in contract.time_window[:2]:
                        lower, upper, encoding = contract.time_window
                        inside = (100, 200) if encoding == "unix_seconds" else (
                            "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z",
                        )
                        outside = (100, 400) if encoding == "unix_seconds" else (
                            "2026-01-02T00:00:00Z", "2026-01-05T00:00:00Z",
                        )
                        arguments = dict(zip((lower, upper), inside))
                        period = {"kind": "utc_range", "start": "2026-01-01T00:00:00Z",
                                  "end": "2026-01-04T00:00:00Z"}
                        if encoding == "unix_seconds":
                            period = {"kind": "utc_range", "start": "1970-01-01T00:01:00Z",
                                      "end": "1970-01-01T00:05:00Z"}
                        from elbow_helper.features.agent.plan.checker import parse_periods
                        periods = parse_periods([period])
                        self.assertEqual(time_check(contract, arguments, periods, set()), "")
                        arguments.update(zip((lower, upper), outside))
                        self.assertTrue(time_check(contract, arguments, periods, set()))
                    elif field in contract.bounded_fields:
                        lower, upper, encoding = contract.time_window
                        arguments = {field: "cursor", lower: 100 if encoding == "unix_seconds"
                                     else "2026-01-02T00:00:00Z",
                                     upper: 200 if encoding == "unix_seconds"
                                     else "2026-01-03T00:00:00Z"}
                        period = (("utc_range", _period_start(encoding),
                                   _period_end(encoding)),)
                        self.assertEqual(time_check(contract, arguments, period, set()), "")
                        self.assertTrue(time_check(contract, {field: "cursor"}, period, set()))
                    elif field in contract.latest_fields:
                        value = schema.get("enum", ["synthetic-key"])[0]
                        if schema.get("type") == "integer":
                            value = 7
                        self.assertEqual(time_check(contract, {field: value}, (), set()), "")
                        self.assertTrue(time_check(contract, {field: value},
                                        (("key", value, field),), set()))
                    else:
                        value = schema.get("enum", ["synthetic-key"])[0]
                        if schema.get("type") == "integer":
                            value = 7
                        self.assertEqual(time_check(contract, {field: value},
                                         (("key", value, field),), set()), "")
                        self.assertTrue(time_check(contract, {field: value},
                                        (("key", "different-key", field),), set()))
                    checked += 1
        self.assertGreater(checked, 0)

    def test_every_entity_field_refuses_other_named_sources(self):
        checked = 0
        for name, tool in self.registry.items():
            contract = CONTRACTS[name]
            for field, kind in contract.entity_fields:
                with self.subTest(capability=name, field=field):
                    schema = tool.definition.parameters["properties"][field]
                    selected = [101] if schema.get("type") == "array" else 101
                    if schema.get("type") == "string":
                        selected = "101"
                    named = {entity_kind(kind): {"101"}}
                    self.assertEqual(source_check(contract, {field: selected}, named,
                                     named, {})[0], "")
                    other = [202] if isinstance(selected, list) else (
                        "202" if isinstance(selected, str) else 202
                    )
                    issue, offered = source_check(contract, {field: other}, named, {}, {})
                    self.assertTrue(issue)
                    self.assertEqual(offered, ("202",))
                    checked += 1
        self.assertGreater(checked, 0)

    def test_natural_language_is_not_a_period(self):
        for value in ("previous interval", "some time", "later", "soon"):
            plan = {"goal": "Read a value", "effort": "low", "output": "text",
                    "periods": [{"kind": "utc_range", "start": value, "end": value}],
                    "entities": [], "steps": []}
            self.assertFalse(check_plan(plan, self.registry).ok)

    def test_full_plans_bound_every_registered_time_field(self):
        for name, tool in self.registry.items():
            contract = CONTRACTS[name]
            for field in contract.time_fields:
                with self.subTest(capability=name, field=field):
                    plan = _plan_for(name, tool, contract, field)
                    check = check_plan(plan, self.registry)
                    self.assertTrue(check.ok, check.error)
                    outside = copy.deepcopy(plan)
                    if field in contract.latest_fields and field not in contract.bounded_fields:
                        outside["periods"] = [{"kind": "key", "field": field,
                                               "value": plan["steps"][0]["arguments"][field]}]
                    elif contract.time_window and (field in contract.time_window[:2] or field in contract.bounded_fields):
                        outside["periods"][0]["start"] = "2030-01-01"
                        outside["periods"][0]["end"] = "2030-01-02"
                    else:
                        for period in outside["periods"]:
                            if period.get("field") == field:
                                period["value"] = "outside"
                    self.assertFalse(check_plan(outside, self.registry).ok)

    def test_full_plans_refuse_other_sources_for_every_entity_field(self):
        for name, tool in self.registry.items():
            if name in {"read_saved_report", "compare_saved_reports"}:
                continue
            contract = CONTRACTS[name]
            for field, kind in contract.entity_fields:
                with self.subTest(capability=name, field=field):
                    plan = _plan_for(name, tool, contract, field)
                    value = plan["steps"][0]["arguments"][field]
                    if isinstance(value, list) and not value:
                        value = [_sample(tool.definition.parameters["properties"][field]["items"])]
                        plan["steps"][0]["arguments"][field] = value
                        plan["entities"].extend({"kind": entity_kind(kind), "value": item} for item in value)
                    named = {entity_kind(kind): frozenset(value if isinstance(value, list) else [value])}
                    self.assertTrue(check_plan(plan, self.registry, named).ok)
                    other = copy.deepcopy(plan)
                    outside = 98765 if type(value) is int else "#P2"
                    choices = tool.definition.parameters["properties"][field].get("enum", ())
                    if len(choices) > 1:
                        outside = next(item for item in choices if item != value)
                    if isinstance(value, list):
                        outside = list(value)
                        item_choices = tool.definition.parameters["properties"][field]["items"].get("enum", ())
                        outside[0] = (next(item for item in item_choices if item not in value) if item_choices else
                                      98765 if type(value[0]) is int else "#P2")
                    other["steps"][0]["arguments"][field] = outside
                    check = check_plan(other, self.registry, named)
                    self.assertFalse(check.ok)
                    self.assertTrue(check.offered)

    def test_initial_and_runtime_checks_agree_for_registered_source_arguments(self):
        for name, tool in self.registry.items():
            if name in {"read_saved_report", "compare_saved_reports"}:
                continue
            contract = CONTRACTS[name]
            for field, _ in contract.entity_fields:
                plan = _plan_for(name, tool, contract, field)
                entities = {}
                for entity in plan["entities"]:
                    entities.setdefault(entity_kind(entity["kind"]), set()).add(str(entity["value"]))
                for invalid in (False, True):
                    candidate = copy.deepcopy(plan)
                    if invalid:
                        candidate["steps"][0]["arguments"][field] = None
                    with self.subTest(capability=name, field=field, invalid=invalid):
                        initial = check_plan(candidate, self.registry)
                        runtime = check_step(
                            candidate["steps"][0], self.registry,
                            parse_periods(candidate["periods"]), entities, {}, set(),
                            resolved=True,
                        )
                        self.assertEqual((initial.ok, initial.error, initial.offered),
                                         (runtime.ok, runtime.error, runtime.offered))

    def test_every_saved_report_kind_checks_its_source_fields(self):
        for name in ("read_saved_report", "compare_saved_reports"):
            kinds = self.registry[name].definition.parameters["properties"]["report_kind"]["enum"]
            for kind in kinds:
                selected = original_tool(self.registry, name, {"report_kind": kind})
                contract = SAVED_REPORT_CONTRACTS[selected.definition.name]
                for field, kind_name in contract.entity_fields:
                    with self.subTest(capability=name, kind=kind, field=field):
                        plan = _plan_for(selected.definition.name, selected, contract, field)
                        plan["steps"][0]["capability"] = name
                        plan["steps"][0]["arguments"]["report_kind"] = kind
                        value = plan["steps"][0]["arguments"][field]
                        named = {entity_kind(kind_name): frozenset([value])}
                        self.assertTrue(check_plan(plan, self.registry, named).ok)
                        other = copy.deepcopy(plan)
                        choices = selected.definition.parameters["properties"][field].get("enum", ())
                        outside = (next(item for item in choices if item != value)
                                   if len(choices) > 1 else
                                   98765 if type(value) is int else "#P2")
                        other["steps"][0]["arguments"][field] = outside
                        check = check_plan(other, self.registry, named)
                        self.assertFalse(check.ok)
                        self.assertTrue(check.offered)

    def test_saved_report_rejects_an_unsupported_filter_with_supported_names(self):
        name = "read_saved_report"
        kind = self.registry[name].definition.parameters["properties"]["report_kind"]["enum"][0]
        selected = original_tool(self.registry, name, {"report_kind": kind})
        contract = SAVED_REPORT_CONTRACTS[selected.definition.name]
        plan = _plan_for(selected.definition.name, selected, contract)
        plan["steps"][0]["capability"] = name
        plan["steps"][0]["arguments"].update(report_kind=kind, unsupported_filter="value")
        check = check_plan(plan, self.registry)
        self.assertFalse(check.ok)
        self.assertIn("unsupported_filter", check.error)
        self.assertIn("Supported filters:", check.error)

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

    def test_historical_windows_cannot_default_to_unbounded_reads(self):
        for name, contract in CONTRACTS.items():
            if contract.time_window and not contract.latest_fields:
                with self.subTest(capability=name):
                    self.assertTrue(time_check(contract, {}, (), set()))
                    self.assertTrue(time_check(contract, {}, (("key", "synthetic-key", "selected"),), set()))

    def test_words_never_become_utc_boundaries(self):
        from elbow_helper.features.agent.plan.checker import _utc
        generator = random.Random(7291)
        for _ in range(1000):
            value = " ".join("".join(generator.choices(string.ascii_letters, k=generator.randrange(1, 12)))
                             for _ in range(generator.randrange(1, 5)))
            with self.assertRaises(ValueError):
                _utc(value)

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
            self.assertEqual(check.ok, value == base["steps"][0][field])
            if not check.ok:
                self.assertTrue(check.error)

    def test_every_period_resolver_requires_its_exact_unpaged_result(self):
        for name, contract in CONTRACTS.items():
            for path in contract.period_results:
                with self.subTest(capability=name, path=path):
                    plan = _plan_for(name, self.registry[name], contract)
                    plan["periods"] = [{"kind": "resolved", "step": "step", "selector": "latest", "path": list(path)}]
                    self.assertTrue(check_plan(plan, self.registry).ok)
                    outside = copy.deepcopy(plan)
                    index = next((index for index, value in enumerate(path) if type(value) is int), None)
                    if index is None:
                        outside["periods"][0]["path"].append("other-key")
                    else:
                        outside["periods"][0]["path"][index] = 7
                    self.assertFalse(check_plan(outside, self.registry).ok)
                    for field in (*contract.latest_fields, *contract.bounded_fields):
                        paged = copy.deepcopy(plan)
                        paged["steps"][0]["arguments"][field] = _sample(self.registry[name].definition.parameters["properties"][field])
                        self.assertFalse(check_plan(paged, self.registry).ok)
