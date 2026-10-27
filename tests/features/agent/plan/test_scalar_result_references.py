"""Synthetic collection references respect the consuming argument's cardinality."""

import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.features.agent.plan.checker import check_plan, check_step, valid_arguments
from elbow_helper.features.agent.plan.executor import (
    SingleValueReferenceError, execute_plan, resolve_arguments,
)
from elbow_helper.infrastructure.ai import AgentToolDefinition


REFERENCE = {"step": "resolve", "path": ["rows", "*", "member_id"]}
INTEGER = {"type": "integer", "minimum": 1}
SCHEMA = {"type": "object", "additionalProperties": False,
          "properties": {"author_id": INTEGER}, "required": ["author_id"]}


def _source(values):
    return {"resolve": {"rows": [{"member_id": value} for value in values]}}


def _step(arguments):
    return {"id": "msgs", "capability": "read_messages", "arguments": arguments,
            "depends_on": []}


def _checked_plan(steps, schema=SCHEMA):
    registry = {"read_messages": RegisteredAgentTool(
        AgentToolDefinition("read_messages", "Synthetic messages.", schema), AsyncMock(),
    )}
    plan = {"goal": "Synthetic messages", "effort": "low", "output": "text", "steps": steps}
    checked = check_plan(plan, registry, completed_steps={
        "resolve": _step({"author_id": 7}),
    })
    return plan, checked


class ScalarResultReferenceTests(unittest.IsolatedAsyncioTestCase):
    def test_single_distinct_value_collapses_without_relaxing_scalar_validation(self):
        for values in ([7], [7, 7, 7], [0, 0], [True, True], ["7", "7"]):
            with self.subTest(values=values):
                arguments = resolve_arguments({"author_id": REFERENCE}, _source(values), schema=SCHEMA)
                self.assertEqual(arguments, {"author_id": values[0]})
                self.assertEqual(valid_arguments(arguments, SCHEMA), type(values[0]) is int and values[0] > 0)

    def test_ambiguous_and_empty_collections_name_argument_without_source_values(self):
        for values in ([719341, 719342], [], [1, True], [1, 1.0]):
            with self.subTest(values=values), self.assertRaises(SingleValueReferenceError) as raised:
                resolve_arguments({"author_id": REFERENCE}, _source(values), schema=SCHEMA)
            message = str(raised.exception)
            self.assertIn("author_id takes one value", message)
            self.assertIn("several distinct values" if values else "no values", message)
            self.assertIn("Choose one value", message)
            self.assertNotIn("719341", message)
            self.assertNotIn("719342", message)

    def test_list_arguments_preserve_values_duplicates_and_expansion(self):
        schema = {**SCHEMA, "properties": {
            "author_id": {"type": "array", "items": INTEGER},
        }}
        for value, expected in ((REFERENCE, [7, 7, 8]), ([9, REFERENCE], [9, 7, 7, 8])):
            with self.subTest(value=value):
                arguments = resolve_arguments({"author_id": value}, _source([7, 7, 8]), schema=schema)
                self.assertEqual(arguments, {"author_id": expected})
                self.assertTrue(valid_arguments(arguments, schema))

    def test_union_schemas_keep_list_alternatives_and_collapse_scalar_alternatives(self):
        for detail, expected in (
            ({"type": ["integer", "null"]}, 7),
            ({"anyOf": [INTEGER, {"type": "null"}]}, 7),
            ({"anyOf": [INTEGER, {"type": "array", "items": INTEGER}]}, [7, 7]),
            ({"type": ["integer", "array"], "items": INTEGER}, [7, 7]),
        ):
            with self.subTest(detail=detail):
                schema = {**SCHEMA, "properties": {"author_id": detail}}
                arguments = resolve_arguments({"author_id": REFERENCE}, _source([7, 7]), schema=schema)
                self.assertEqual(arguments, {"author_id": expected})
                self.assertTrue(valid_arguments(arguments, schema))

    def test_nested_scalar_error_names_the_full_argument_path(self):
        schema = {"type": "object", "properties": {"filters": {
            "type": "array", "items": SCHEMA,
        }}}
        with self.assertRaisesRegex(SingleValueReferenceError, r"filters\[0\]\.author_id takes one value"):
            resolve_arguments({"filters": [{"author_id": REFERENCE}]}, _source([7, 8]), schema=schema)
        self.assertEqual(resolve_arguments(
            {"filters": [{"author_id": REFERENCE}]}, _source([7, 7]), schema=schema,
        ), {"filters": [{"author_id": 7}]})

    def test_non_wildcard_references_and_literal_lists_are_not_collapsed(self):
        reference = {"step": "resolve", "path": ["values"]}
        for value in (reference, [7, 7]):
            arguments = resolve_arguments({"author_id": value}, {"resolve": {"values": [7, 7]}}, schema=SCHEMA)
            self.assertEqual(arguments, {"author_id": [7, 7]})
            self.assertFalse(valid_arguments(arguments, SCHEMA))

    def test_checker_accepts_wildcard_scalar_reference_without_plan_rejection(self):
        registry = {"read_messages": RegisteredAgentTool(
            AgentToolDefinition("read_messages", "Synthetic messages.", SCHEMA), AsyncMock(),
        )}
        step = _step({"author_id": REFERENCE})
        checked = check_step(step, registry)
        self.assertTrue(checked.ok, checked.error)
        _, checked = _checked_plan([step])
        self.assertTrue(checked.ok, checked.error)
        self.assertEqual(checked.step_errors, {})
        step["arguments"]["author_id"] = {"step": "resolve", "path": ["rows", 0, "member_id"]}
        self.assertTrue(check_step(step, registry).ok)

    def test_checker_preserves_direct_and_embedded_list_references(self):
        schema = {**SCHEMA, "properties": {
            "author_id": {"type": "array", "items": INTEGER},
        }}
        registry = {"read_messages": RegisteredAgentTool(
            AgentToolDefinition("read_messages", "Synthetic messages.", schema), AsyncMock(),
        )}
        for value in (REFERENCE, [REFERENCE]):
            self.assertTrue(check_step(_step({"author_id": value}), registry).ok)

    async def test_runtime_guard_refuses_ambiguous_or_empty_step_and_blocks_only_its_dependents(self):
        plan, checked = _checked_plan([
            _step({"author_id": REFERENCE}),
            {"id": "later", "capability": "read_messages", "arguments": {"author_id": 9}, "depends_on": ["msgs"]},
            {"id": "other", "capability": "read_messages", "arguments": {"author_id": 9}, "depends_on": []},
        ])
        self.assertTrue(checked.ok, checked.error)
        self.assertEqual(checked.step_errors, {})
        for values in ([7, 8], []):
            with self.subTest(values=values):
                run = AsyncMock(return_value={"messages": []})
                results = await execute_plan(
                    plan, run, earlier_results=_source(values), step_errors=checked.step_errors,
                    argument_schema=lambda step: SCHEMA,
                )
                self.assertIn("author_id takes one value", results["msgs"]["error"])
                self.assertIn("several distinct values" if values else "no values", results["msgs"]["error"])
                self.assertEqual(results["msgs"]["cause"], "invalid_arguments")
                self.assertEqual(results["msgs"]["capability"], "read_messages")
                self.assertEqual(results["msgs"]["argument_schema"], SCHEMA)
                self.assertIn("Step msgs failed", results["later"]["error"])
                self.assertEqual(results["other"], {"messages": []})
                run.assert_awaited_once()
                self.assertEqual(run.await_args.args[0]["id"], "other")

    async def test_runtime_passes_single_distinct_value_to_handler(self):
        plan, checked = _checked_plan([_step({"author_id": REFERENCE})])
        self.assertTrue(checked.ok, checked.error)
        self.assertEqual(checked.step_errors, {})
        for values in ([7], [7, 7, 7]):
            with self.subTest(values=values):
                run = AsyncMock(return_value={"messages": []})
                results = await execute_plan(
                    plan, run, earlier_results=_source(values), step_errors=checked.step_errors,
                    argument_schema=lambda step: SCHEMA,
                )
                self.assertEqual(results["msgs"], {"messages": []})
                run.assert_awaited_once()
                self.assertEqual(run.await_args.args[1], {"author_id": 7})

    async def test_runtime_preserves_list_arguments(self):
        schema = {**SCHEMA, "properties": {
            "author_id": {"type": "array", "items": INTEGER},
        }}
        for value, expected in ((REFERENCE, [7, 7, 8]), ([9, REFERENCE], [9, 7, 7, 8])):
            with self.subTest(value=value):
                plan, checked = _checked_plan([_step({"author_id": value})], schema)
                self.assertTrue(checked.ok, checked.error)
                self.assertEqual(checked.step_errors, {})
                run = AsyncMock(return_value={"messages": []})
                results = await execute_plan(
                    plan, run, earlier_results=_source([7, 7, 8]), step_errors=checked.step_errors,
                    argument_schema=lambda step: schema,
                )
                self.assertEqual(results["msgs"], {"messages": []})
                run.assert_awaited_once()
                self.assertEqual(run.await_args.args[1], {"author_id": expected})
