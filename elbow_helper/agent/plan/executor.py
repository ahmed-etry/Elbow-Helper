"""Run checked capability steps without model calls between them."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .checker import _valid_value, step_dependencies
from .arguments import takes_single_value
from .results import is_record_table


LOGGER = logging.getLogger(__name__)


class UnresolvedReferenceError(KeyError):
    """Keep the failed reference identity without retaining result values."""

    def __init__(self, step_id, path):
        super().__init__("Unresolved earlier result reference")
        self.step_id = step_id
        self.path = path


class SingleValueReferenceError(ValueError):
    """A collection cannot supply an unambiguous single argument value."""


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
    def resolve(value, detail, field="arguments", *, expand=False):
        reference = isinstance(value, dict) and set(value) == {"step", "path"}
        if not reference and "anyOf" in detail and "type" not in detail:
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
                collected = _walk(source, value["path"])
            except (KeyError, IndexError, TypeError):
                raise UnresolvedReferenceError(value["step"], value["path"]) from None
            if "*" in value["path"] and not expand and takes_single_value(detail):
                if not collected or any(type(item) is not type(collected[0]) or item != collected[0]
                                        for item in collected[1:]):
                    reason = "several distinct values" if collected else "no values"
                    raise SingleValueReferenceError(
                        f"{field} takes one value, but its result reference collected {reason}. "
                        "Choose one value or ask for clarification."
                    )
                return collected[0]
            return collected
        if isinstance(value, dict):
            properties = detail.get("properties", {})
            additional = detail.get("additionalProperties", {})
            resolved = {name: resolve(item, properties.get(
                        name, additional if isinstance(additional, Mapping) else {}),
                        name if field == "arguments" else f"{field}.{name}")
                    for name, item in value.items()}
            member_ids = value.get("member_ids")
            references = (member_ids if isinstance(member_ids, list) else [member_ids])
            if (isinstance(resolved.get("member_ids"), list) and any(
                    isinstance(item, dict) and set(item) == {"step", "path"} for item in references)):
                identifiers = []
                for identifier in resolved["member_ids"]:
                    if identifier not in identifiers:
                        identifiers.append(identifier)
                resolved["member_ids"] = identifiers
            return resolved
        if isinstance(value, list):
            items = []
            for index, item in enumerate(value):
                resolved = resolve(item, detail.get("items", {}), f"{field}[{index}]", expand=True)
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
            return step["id"], {
                "error": step_errors[step["id"]], "cause": "invalid_arguments",
                "capability": step["capability"],
                **({"argument_schema": argument_schema(step)} if argument_schema else {}),
            }
        try:
            arguments = resolve_arguments(
                step["arguments"], results,
                schema=argument_schema(step) if argument_schema else None,
            )
        except UnresolvedReferenceError as error:
            LOGGER.warning("Agent step %s has unresolved reference to step %s path %s",
                           step["id"], error.step_id, error.path)
            return step["id"], {"error": "A required earlier result is unavailable."}
        except SingleValueReferenceError as error:
            return step["id"], {
                "error": str(error), "cause": "invalid_arguments",
                "capability": step["capability"],
                **({"argument_schema": argument_schema(step)} if argument_schema else {}),
            }
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
