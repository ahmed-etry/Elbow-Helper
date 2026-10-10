"""Synthetic references supply dependencies without duplicate ordering fields."""

import asyncio
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.features.agent.plan.checker import check_plan
from elbow_helper.features.agent.plan.executor import execute_plan
from elbow_helper.infrastructure.ai import AgentToolDefinition


def _step(identity, capability="read_items", arguments=None, dependencies=()):
    return {"id": identity, "capability": capability, "arguments": arguments or {},
            "depends_on": list(dependencies)}


def _plan(steps):
    return {"goal": "Synthetic values", "effort": "low", "output": "text", "steps": steps}


class PlanDependencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        schema = {"type": "object", "additionalProperties": False, "properties": {
            "value": {"type": "integer"},
            "nested": {"type": "object", "properties": {
                "values": {"type": "array", "items": {"type": "integer"}},
            }},
        }}
        self.registry = {
            name: RegisteredAgentTool(AgentToolDefinition(name, "Synthetic values.", schema),
                                      AsyncMock(), action_class=classification)
            for name, classification in (
                ("read_items", ActionClass.READ), ("write_table", ActionClass.OUTPUT),
                ("change_value", ActionClass.CHANGE),
                ("irreversible_value", ActionClass.IRREVERSIBLE),
            )
        }

    async def test_nested_references_and_explicit_ordering_both_wait_for_results(self):
        for dependencies in ([], ["other"]):
            with self.subTest(dependencies=dependencies):
                reference = {"step": "source", "path": ["value"]}
                plan = _plan([
                    _step("source"), _step("other"),
                    _step("consumer", arguments={"nested": {"values": [reference]}},
                          dependencies=dependencies),
                ])
                checked = check_plan(plan, self.registry)
                self.assertTrue(checked.ok, checked.error)
                self.assertFalse(checked.step_errors)
                finished = []

                async def run(step, arguments, results):
                    if step["id"] == "consumer":
                        self.assertIn("source", finished)
                        if dependencies:
                            self.assertIn("other", finished)
                        self.assertEqual(arguments, {"nested": {"values": [7]}})
                    await asyncio.sleep(0)
                    finished.append(step["id"])
                    return {"value": 7}

                results = await execute_plan(plan, run)
                self.assertEqual(results["consumer"], {"value": 7})

    async def test_reference_to_a_failed_step_stops_its_dependent(self):
        plan = _plan([
            _step("source"), _step("consumer", arguments={
                "value": {"step": "source", "path": ["value"]},
            }),
        ])
        run = AsyncMock(return_value={"error": "Synthetic failure"})
        results = await execute_plan(plan, run)
        run.assert_awaited_once()
        self.assertEqual(results["consumer"], {
            "error": "Step source failed, so this step could not run.",
        })

    def test_unknown_later_and_malformed_references_still_reject_the_plan(self):
        references = [
            {"step": "unknown", "path": ["value"]},
            {"step": "later", "path": ["value"]},
            {"step": "consumer", "path": ["value"]},
            {"step": [], "path": ["value"]},
            *({"step": "source", "path": path}
              for path in ([], "value", [-1], [True], [""], [None], ["value"] * 9)),
        ]
        for reference in references:
            with self.subTest(reference=reference):
                checked = check_plan(_plan([
                    _step("source"), _step("consumer", arguments={"value": reference}),
                    _step("later"),
                ]), self.registry)
                self.assertFalse(checked.ok)
                self.assertEqual(checked.step_id, "consumer")

    def test_reads_cannot_reference_changes_through_outputs_or_completed_steps(self):
        for capability in ("change_value", "irreversible_value"):
            for indirect in (False, True):
                for completed in (False, True):
                    with self.subTest(
                        capability=capability, indirect=indirect, completed=completed,
                    ):
                        steps = [_step("change", capability)]
                        if indirect:
                            steps.append(_step("output", "write_table", {
                                "value": {"step": "change", "path": ["value"]},
                            }))
                        reader = _step("reader", arguments={
                            "value": {"step": steps[-1]["id"], "path": ["value"]},
                        })
                        previous = {step["id"]: step for step in steps} if completed else {}
                        checked = check_plan(
                            _plan([reader] if completed else [*steps, reader]), self.registry,
                            completed_steps=previous,
                        )
                        self.assertFalse(checked.ok)
                        self.assertEqual(checked.step_id, "reader")
                        self.assertEqual(checked.error,
                            "Plan reads before changes; a read can't use a change's result.")

    async def test_revision_references_a_completed_read_without_explicit_ordering(self):
        plan = _plan([_step("consumer", arguments={
            "value": {"step": "source", "path": ["value"]},
        })])
        checked = check_plan(plan, self.registry, completed_steps={"source": _step("source")})
        self.assertTrue(checked.ok, checked.error)
        self.assertFalse(checked.step_errors)
        run = AsyncMock(return_value={"value": 7})
        result = await execute_plan(plan, run, earlier_results={"source": {"value": 7}})
        run.assert_awaited_once()
        self.assertEqual(run.await_args.args[1], {"value": 7})
        self.assertEqual(result["consumer"], {"value": 7})
