"""Run checked capability steps without model calls between them."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .checker import _valid_value, step_dependencies
from .results import is_record_table


LOGGER = logging.getLogger(__name__)


class UnresolvedReferenceError(KeyError):
    """Keep the failed reference identity without retaining result values."""

    def __init__(self, step_id, path):
        super().__init__("Unresolved earlier result reference")
        self.step_id = step_id
        self.path = path


def _walk(source: Any, path: list[Any]) -> Any:
    for index, part in enumerate(path):
        if part == "rows" and is_record_table(source):
            continue
        if part == "*":
            if not isinstance(source, list):
                raise TypeError("Only a list can be expanded")
            rest = path[index + 1:]
            collected = [_walk(item, rest) for item in source]
            return [value for group in collected for value in group] if "*" in rest else collected
        source = source[part]
    return source


def resolve_arguments(
    arguments: Mapping[str, Any], results: Mapping[str, Mapping[str, Any]],
    *, schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    def resolve(value, detail):
        if "anyOf" in detail and "type" not in detail:
            detail = next((choice for choice in detail["anyOf"]
                           if _valid_value(value, choice, set(results))), {})
        if (detail.get("x-result-list") and isinstance(value, list) and len(value) == 1
                and isinstance(value[0], dict) and set(value[0]) == {"step", "path"}):
            value = value[0]
        if isinstance(value, dict) and set(value) == {"step", "path"}:
            try:
                source = results[value["step"]]
                if source.get("status") == "confirmation_required":
                    return value
                return _walk(source, value["path"])
            except (KeyError, IndexError, TypeError):
                raise UnresolvedReferenceError(value["step"], value["path"]) from None
        if isinstance(value, dict):
            properties = detail.get("properties", {})
            return {field: resolve(item, properties.get(field, {}))
                    for field, item in value.items()}
        if isinstance(value, list):
            items = []
            for item in value:
                resolved = resolve(item, detail.get("items", {}))
                expanded = (isinstance(item, dict) and set(item) == {"step", "path"}
                            and "*" in item["path"] and isinstance(resolved, list))
                items.extend(resolved) if expanded else items.append(resolved)
            return items
        return value
    return resolve(dict(arguments), schema or {})


async def execute_plan(
    plan: Mapping[str, Any],
    run: Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
    *, max_concurrency: int = 4,
    earlier_results: Mapping[str, Mapping[str, Any]] | None = None,
    parallel: Callable[[Mapping[str, Any]], bool] | None = None,
    step_errors: Mapping[str, str] | None = None,
    argument_schema: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Mapping[str, Any]]:
    """Run independent steps together and wait for dependencies."""
    if max_concurrency < 1:
        raise ValueError("Concurrency must be positive")
    steps = {step["id"]: step for step in plan["steps"]}
    pending = set(steps)
    results: dict[str, Mapping[str, Any]] = {
        step_id: result for step_id, result in (earlier_results or {}).items()
        if step_id not in steps
    }
    semaphore = asyncio.Semaphore(max_concurrency)

    async def run_step(step: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
        for dependency in step_dependencies(step):
            if "error" in results[dependency]:
                return step["id"], {
                    "error": f"Step {dependency} failed, so this step could not run.",
                }
        if step["id"] in (step_errors or {}):
            return step["id"], {"error": step_errors[step["id"]]}
        try:
            arguments = resolve_arguments(
                step["arguments"], results,
                schema=argument_schema(step) if argument_schema else None,
            )
        except UnresolvedReferenceError as error:
            LOGGER.warning("Agent step %s has unresolved reference to step %s path %s",
                           step["id"], error.step_id, error.path)
            return step["id"], {"error": "A required earlier result is unavailable."}
        async with semaphore:
            return step["id"], await run(step, arguments, results)

    while pending:
        ready = [steps[step_id] for step_id in steps if step_id in pending
                 and set(step_dependencies(steps[step_id])) <= results.keys()]
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
    return {step_id: results[step_id] for step_id in steps}
