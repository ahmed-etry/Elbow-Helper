"""Dependency execution and cancellation use synthetic steps."""

import asyncio
import unittest

from elbow_helper.features.agent.plan.executor import (
    UnresolvedReferenceError, execute_plan, resolve_arguments,
)


class PlanExecutorTests(unittest.IsolatedAsyncioTestCase):
    def test_nested_wildcards_flatten_only_expanded_indexes(self):
        source = {"groups": [{"members": [{"accounts": [{"tag": "#P0"}, {"tag": "#P2"}]}]},
                             {"members": []},
                             {"members": [{"accounts": []}, {"accounts": [{"tag": "#P8"}]}]}]}
        reference = {"step": "source", "path": ["groups", "*", "members", "*", "accounts", "*", "tag"]}
        self.assertEqual(resolve_arguments({"tags": reference}, {"source": source}),
                         {"tags": ["#P0", "#P2", "#P8"]})
        self.assertEqual(resolve_arguments({"tags": ["#P9", reference]}, {"source": source}),
                         {"tags": ["#P9", "#P0", "#P2", "#P8"]})
        self.assertEqual(resolve_arguments({"rows": {"step": "source", "path": ["groups", "*", "members"]}},
                                           {"source": source}),
                         {"rows": [group["members"] for group in source["groups"]]})
        source["groups"][0]["members"] = None
        with self.assertRaises(UnresolvedReferenceError):
            resolve_arguments({"tags": reference}, {"source": source})

    def test_action_result_reference_waits_for_confirmed_run(self):
        reference = {"step": "created", "path": ["target_id"]}
        arguments = {"target_id": reference}
        self.assertEqual(resolve_arguments(arguments, {
            "created": {"status": "confirmation_required"},
        }), arguments)
        self.assertEqual(resolve_arguments(arguments, {
            "created": {"target_id": 7},
        }), {"target_id": 7})

    async def test_ready_reads_finish_before_change_previews(self):
        order = []
        async def run(step, arguments, earlier):
            order.append(step["id"])
            return {"value": step["id"]}
        plan = {"steps": [
            {"id": "change", "arguments": {}, "depends_on": []},
            {"id": "read", "arguments": {}, "depends_on": []},
        ]}
        await execute_plan(plan, run, parallel=lambda step: step["id"] == "read")
        self.assertEqual(order, ["read", "change"])

    async def test_failed_batch_waits_for_other_steps_before_returning(self):
        finished = asyncio.Event()
        async def run(step, arguments, earlier):
            if step["id"] == "first":
                raise RuntimeError("synthetic_failure")
            await asyncio.sleep(0)
            finished.set()
            return {"value": 7}
        plan = {"steps": [{"id": value, "arguments": {}, "depends_on": []}
                          for value in ("first", "second")]}
        with self.assertRaisesRegex(RuntimeError, "synthetic_failure"):
            await execute_plan(plan, run)
        self.assertTrue(finished.is_set())

    async def test_cancelled_batch_waits_for_every_step_to_stop(self):
        started = asyncio.Event()
        stopped = []
        async def run(step, arguments, earlier):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(step["id"])
        plan = {"steps": [{"id": value, "arguments": {}, "depends_on": []}
                          for value in ("first", "second")]}
        task = asyncio.create_task(execute_plan(plan, run))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertCountEqual(stopped, ["first", "second"])
