"""Run checked capability steps without model calls between them."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any


def resolve_arguments(
    arguments: Mapping[str, Any], results: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    def resolve(value):
        if isinstance(value, dict) and set(value) == {"step", "path"}:
            source = results[value["step"]]
            for part in value["path"]:
                source = source[part]
            return source
        if isinstance(value, dict):
            return {field: resolve(item) for field, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value
    return resolve(dict(arguments))


async def execute_plan(
    plan: Mapping[str, Any],
    run: Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
    *, max_concurrency: int = 4,
    parallel: Callable[[Mapping[str, Any]], bool] | None = None,
) -> dict[str, Mapping[str, Any]]:
    """Run independent steps together and wait for dependencies."""
    if max_concurrency < 1:
        raise ValueError("Concurrency must be positive")
    steps = {step["id"]: step for step in plan["steps"]}
    pending = set(steps)
    results: dict[str, Mapping[str, Any]] = {}
    semaphore = asyncio.Semaphore(max_concurrency)

    async def run_step(step: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
        try:
            arguments = resolve_arguments(step["arguments"], results)
        except (KeyError, IndexError, TypeError):
            return step["id"], {"error": "A required earlier result is unavailable."}
        async with semaphore:
            return step["id"], await run(step, arguments, results)

    while pending:
        ready = [steps[step_id] for step_id in steps if step_id in pending
                 and set(steps[step_id]["depends_on"]) <= results.keys()]
        if not ready:
            raise ValueError("Step dependencies cannot be resolved")
        if parallel is not None:
            reads = [step for step in ready if parallel(step)]
            ready = reads or [ready[0]]
        tasks = [asyncio.create_task(run_step(step)) for step in ready]
        try:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        for step_id, result in outcomes:
            results[step_id] = result
            pending.remove(step_id)
    return results
