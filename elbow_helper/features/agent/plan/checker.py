"""Validate plan shape, schemas, dependencies and confirmation boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable, Mapping
from typing import Any
import logging
import re
import math

from ..engine.capability_contract import (
    CapabilityContract,
    CapabilityBindError,
    compile_capability_call,
)
from ..models import RegisteredAgentTool
from ..actions.contracts import ActionClass
from ..reports.tools import (COMPARE_NAME, READ_NAME, original_arguments, original_tool,
                            unsupported_fields, unsupported_field_error)
from .format import output_forms
from .arguments import argument_errors

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlanCheck:
    ok: bool
    error: str = ""
    step_id: str = ""
    offered: tuple[str, ...] = ()
    step_errors: Mapping[str, str] = field(default_factory=dict)


def _error(message, step_id="", offered=()):
    return PlanCheck(False, message, step_id, offered)


def _valid_value(
    value: Any, schema: Mapping[str, Any], dependencies: set[str] | None = None,
) -> bool:
    if dependencies is not None and isinstance(value, dict) and set(value) == {"step", "path"}:
        return _reference(value, dependencies)
    if "enum" in schema and value not in schema["enum"]:
        return False
    kind = schema.get("type")
    if kind == "string":
        return (isinstance(value, str) and (bool(value.strip()) or schema.get("minLength") == 0)
                and ("pattern" not in schema or re.search(schema["pattern"], value) is not None)
                and schema.get("minLength", 1) <= len(value) <= schema.get("maxLength", 2**31))
    if kind == "integer":
        return (type(value) is int and schema.get("minimum", -(2**63)) <= value
                <= schema.get("maximum", 2**64 - 1))
    if kind == "number":
        return (
            type(value) in (int, float) and math.isfinite(value)
            and schema.get("minimum", float("-inf")) <= value
            <= schema.get("maximum", float("inf"))
        )
    if kind == "boolean":
        return type(value) is bool
    if kind == "array":
        return (isinstance(value, list) and schema.get("minItems", 0) <= len(value)
                <= schema.get("maxItems", 2**31)
                and all(_valid_value(item, schema["items"], dependencies) for item in value)
                and (not schema.get("uniqueItems")
                     or len({str(item) for item in value}) == len(value)))
    if kind == "object":
        return valid_arguments(value, schema, dependencies)
    return False


def valid_arguments(
    arguments: Any, schema: Mapping[str, Any], dependencies: set[str] | None = None,
) -> bool:
    if not isinstance(arguments, dict):
        return False
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        return False
    if (
        set(arguments) - set(properties) and schema.get("additionalProperties") is not True
    ) or set(schema.get("required", ())) - set(arguments):
        return False
    return all(
        _valid_value(value, properties[field], dependencies)
        for field, value in arguments.items() if field in properties
    )


def has_reference(value: Any) -> bool:
    if isinstance(value, dict):
        return set(value) == {"step", "path"} or any(has_reference(item) for item in value.values())
    return isinstance(value, list) and any(has_reference(item) for item in value)


def result_references(value: Any):
    if isinstance(value, dict):
        if set(value) == {"step", "path"}:
            yield value
        else:
            for item in value.values():
                yield from result_references(item)
    elif isinstance(value, list):
        for item in value:
            yield from result_references(item)


def _reference(value: Any, earlier: set[str]) -> bool:
    return (isinstance(value, dict) and set(value) == {"step", "path"}
            and isinstance(value["step"], str) and value["step"] in earlier
            and isinstance(value["path"], list)
            and 1 <= len(value["path"]) <= 8
            and all(isinstance(part, str) and part or type(part) is int and part >= 0
                    for part in value["path"]))


@dataclass(frozen=True, slots=True)
class StepCheck(PlanCheck):
    tool: RegisteredAgentTool | None = None
    contract: CapabilityContract | None = None
    scope: Mapping[str, Any] | None = None


def check_step(
    step: Mapping[str, Any], registry: Mapping[str, RegisteredAgentTool],
    *, resolved: bool = False,
    validate_scope: Callable[[RegisteredAgentTool], str] | None = None,
) -> PlanCheck:
    """Check step arguments and evidence scope before binding a capability call."""
    capability = step["capability"]
    step_id = step["id"]
    dependencies = step["depends_on"]
    scope = None
    tool = registry[capability]
    arguments = step["arguments"]
    if not isinstance(arguments, dict):
        return _error("Use an argument object.", step_id)
    references = {field: value for field, value in arguments.items()
                  if has_reference(value)}
    schema = tool.definition.parameters
    selected = (
        original_tool(registry, capability, arguments)
        if capability in (READ_NAME, COMPARE_NAME) else None
    )
    if selected is not None and unsupported_fields(selected, arguments):
        return _error(unsupported_field_error(selected, arguments), step_id)
    for reference in result_references(arguments):
        if not isinstance(reference.get("step"), str) or reference["step"] not in set(dependencies):
            return _error(
                f"Step {step_id} uses results of step {reference.get('step')!r}, which must be an "
                "completed step listed in depends_on.", step_id,
            )
    if not valid_arguments(arguments, schema, set(dependencies)):
        issues = argument_errors(arguments, schema, set(dependencies), _valid_value)
        return _error("Invalid arguments: " + "; ".join(issues), step_id)
    if capability in (READ_NAME, COMPARE_NAME):
        if selected is None or not valid_arguments(
            original_arguments(arguments), selected.definition.parameters,
            set(dependencies),
        ):
            return _error("Use the fields supported by this report kind.", step_id)
    if validate_scope is not None:
        issue = validate_scope(selected or tool)
        if issue:
            return _error(issue, step_id)
    contract = (selected or tool).contract
    if resolved or (not references and contract is not None):
        try:
            scope = compile_capability_call(
                selected or tool,
                original_arguments(arguments) if selected else arguments,
                contract=contract,
            )
        except CapabilityBindError as error:
            return _error(str(error), step_id)
    return StepCheck(True, tool=tool, contract=contract, scope=scope)


def _mixes_irreversible_changes(steps, registry) -> bool:
    changes = [step for step in steps if registry[step["capability"]].action_class in (
        ActionClass.CHANGE, ActionClass.IRREVERSIBLE,
    )]
    return len(changes) > 1 and any(
        registry[step["capability"]].action_class is ActionClass.IRREVERSIBLE for step in changes
    ) and not all(
        registry[step["capability"]].action_class is ActionClass.IRREVERSIBLE
        and step["capability"] == changes[0]["capability"] for step in changes
    )


def check_plan(raw, registry, *, completed_steps=None):
    """Return a correctable structural error; data errors belong to execution."""
    try:
        if not isinstance(raw, dict) or set(raw) != {"goal", "effort", "output", "steps"}:
            return _error("Supply goal, effort, output and steps.")
        if not isinstance(raw["goal"], str) or not 1 <= len(raw["goal"].strip()) <= 240:
            return _error("Write a short goal.")
        if raw["effort"] not in ("low", "high", "max"):
            return _error("Choose low, high or max effort.")
        if raw["output"] not in output_forms(registry):
            return _error("Choose an available output form.")
        if not isinstance(raw["steps"], list) or not 1 <= len(raw["steps"]) <= 48:
            return _error("List between 1 and 48 steps.")
        completed = completed_steps or {}
        earlier = set(completed)
        step_errors = {}
        after_change = {key for key, step in completed.items()
                        if registry[step["capability"]].action_class in (
                            ActionClass.CHANGE, ActionClass.IRREVERSIBLE,
                        )}
        for step in raw["steps"]:
            if not isinstance(step, dict) or not {
                "id", "capability", "arguments", "depends_on",
            } <= set(step):
                return _error("Each step needs id, capability, arguments and depends_on.")
            identity = step["id"]
            if isinstance(identity, str) and identity in completed:
                return _error("Use a new step ID.", identity)
            if not isinstance(identity, str) or not 1 <= len(identity) <= 40 or identity in earlier:
                return _error("Give each step a unique short ID.")
            name = step["capability"]
            if not isinstance(name, str) or name not in registry:
                return _error("Choose a registered capability.", identity)
            dependencies = step["depends_on"]
            if not isinstance(dependencies, list) or any(
                not isinstance(dep, str) or dep not in earlier for dep in dependencies
            ):
                return _error("Depend only on earlier or completed steps.", identity)
            for reference in result_references(step["arguments"]):
                if not _reference(reference, set(dependencies)):
                    return _error(
                        "Use a valid result reference to a step listed in depends_on.", identity,
                    )
            classification = registry[name].action_class
            depends_on_change = bool(after_change.intersection(dependencies))
            if classification is ActionClass.READ and depends_on_change:
                return _error(
                    "Plan reads before changes; a read can't use a change's result.", identity,
                )
            if depends_on_change or classification in (
                ActionClass.CHANGE, ActionClass.IRREVERSIBLE,
            ):
                after_change.add(identity)
            checked = check_step(step, registry)
            if not checked.ok:
                step_errors[identity] = checked.error
            earlier.add(identity)
        if _mixes_irreversible_changes(raw["steps"], registry):
            return _error("Only irreversible changes of the same kind may share a preview.")
        if raw["output"] != "text" and not any(
            step["capability"] == raw["output"] for step in raw["steps"]
        ):
            return _error("Include the selected output capability in the steps.")
        return PlanCheck(True, step_errors=step_errors)
    except Exception:
        LOGGER.exception("Agent plan check failed unexpectedly")
        return _error("Correct the plan fields and values.")
