"""Dependency execution and cancellation use synthetic steps."""

import asyncio
import unittest

from elbow_helper.features.agent.plan.executor import execute_plan


class PlanExecutorTests(unittest.IsolatedAsyncioTestCase):
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
