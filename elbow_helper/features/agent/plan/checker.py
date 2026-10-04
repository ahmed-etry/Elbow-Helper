"""Check step arguments against capability schemas and request boundaries."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from datetime import date, datetime, time, timezone
from collections.abc import Callable, Mapping
from typing import Any

from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool
from ..actions.contracts import ActionClass
from ..engine.capability_contract import CapabilityBindError
from ..engine.capability_contract import bound_time_window
from ..engine.capability_contract import compile_capability_call
from ..engine.capability_contract import entity_kind
from ..engine.capability_contract import result_path_matches
from ..reports.tools import (COMPARE_NAME, READ_NAME, original_arguments,
                                   original_tool, unsupported_fields,
                                   unsupported_field_error, saved_report_contracts)
from .format import output_forms

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlanCheck:
    ok: bool
    error: str = ""
    step_id: str = ""
    offered: tuple[str, ...] = ()


def _error(message: str, step_id: str = "", offered: tuple[str, ...] = ()) -> PlanCheck:
    return PlanCheck(False, message, step_id, offered)


def _utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Use an explicit UTC date.")
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time.min, timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("Use an explicit UTC date.")
    return parsed.astimezone(timezone.utc)


def parse_periods(raw: Any) -> tuple[tuple[str, Any, Any], ...]:
    if not isinstance(raw, list) or len(raw) > 12:
        raise ValueError("List at most 12 periods.")
    result = []
    for period in raw:
        if not isinstance(period, dict):
            raise ValueError("Each period needs a kind and value.")
        kind = period.get("kind")
        if kind == "utc_range" and set(period) == {"kind", "start", "end"}:
            start, end = _utc(period["start"]), _utc(period["end"])
            if start >= end:
                raise ValueError("A period start must precede its end.")
            result.append((kind, start, end))
        elif kind == "key" and set(period) == {"kind", "field", "value"}:
            value = period["value"]
            if not isinstance(period["field"], str) or not period["field"] or type(value) not in (str, int) or not str(value).strip():
                raise ValueError("A period key needs a value.")
            result.append((kind, value, period["field"]))
        elif kind == "resolved" and set(period) == {"kind", "step", "selector", "path"}:
            if period["selector"] not in ("latest", "current") or not isinstance(period["step"], str):
                raise ValueError("Resolve the latest or current key with an earlier step.")
            if not isinstance(period["path"], list) or not 1 <= len(period["path"]) <= 8 or any(
                type(part) not in (str, int) for part in period["path"]
            ):
                raise ValueError("Give the exact result path for the resolved period.")
            result.append((kind, period["step"], period["path"]))
        else:
            raise ValueError("Use a UTC range, a period key, or an earlier resolved key.")
    return tuple(result)


def _valid_value(value: Any, schema: Mapping[str, Any], dependencies: set[str] | None = None) -> bool:
    if dependencies is not None and isinstance(value, dict) and set(value) == {"step", "path"}:
        return _reference(value, dependencies)
    if "enum" in schema and value not in schema["enum"]:
        return False
    kind = schema.get("type")
    if kind == "string":
        return (isinstance(value, str) and bool(value.strip())
                and schema.get("minLength", 1) <= len(value) <= schema.get("maxLength", 1024))
    if kind == "integer":
        return (type(value) is int and schema.get("minimum", 1) <= value
                <= schema.get("maximum", 2**64 - 1))
    if kind == "boolean":
        return type(value) is bool
    if kind == "array":
        return (isinstance(value, list) and schema.get("minItems", 0) <= len(value)
                <= schema.get("maxItems", 20)
                and all(_valid_value(item, schema["items"], dependencies) for item in value)
                and (not schema.get("uniqueItems") or len({str(item) for item in value}) == len(value)))
    if kind == "object":
        return valid_arguments(value, schema, dependencies)
    return False


def valid_arguments(arguments: Any, schema: Mapping[str, Any], dependencies: set[str] | None = None) -> bool:
    if not isinstance(arguments, dict):
        return False
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        return False
    if set(arguments) - set(properties) or set(schema.get("required", ())) - set(arguments):
        return False
    return all(_valid_value(value, properties[field], dependencies) for field, value in arguments.items())


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
            and isinstance(value["step"], str) and value["step"] in earlier and isinstance(value["path"], list)
            and 1 <= len(value["path"]) <= 8
            and all(isinstance(part, str) and part or type(part) is int and part >= 0
                    for part in value["path"]))


def reference_contract(reference, steps, registry):
    owner = steps[reference["step"]]
    tool = registry[owner["capability"]]
    if owner["capability"] in (READ_NAME, COMPARE_NAME):
        tool = original_tool(registry, owner["capability"], owner["arguments"]) or tool
    return tool.contract


def check_entity_references(contract, arguments, steps, registry, step_id="") -> PlanCheck:
    for field, kind in contract.entity_fields:
        expected = entity_kind(kind)
        for reference in result_references(arguments.get(field)):
            owner = reference_contract(reference, steps, registry)
            actual = owner.result_path_kind(reference["path"]) if owner else None
            if "*" in reference["path"] and not kind.endswith("_set"):
                return _error(
                    f"Argument {field} takes one {expected}; use an index instead of * in "
                    f"path {reference['path']!r}.", step_id,
                )
            if actual != expected:
                return _error(
                    f"Argument {field} expects {expected}; reference to step {reference['step']} "
                    f"path {reference['path']!r} has kind {actual or 'untyped'}.", step_id,
                )
    return PlanCheck(True)


def entity_values(value):
    return value if isinstance(value, list) else [value]


def _check_result_references(
    value: Any, steps: Mapping[str, Mapping[str, Any]],
    registry: Mapping[str, RegisteredAgentTool], consumer: str = "",
) -> PlanCheck:
    if isinstance(value, dict) and set(value) == {"step", "path"}:
        if not _reference(value, set(steps)):
            return _error("Resolve each reference with an earlier step and result path.", consumer)
        if value["path"].count("*") > 1:
            return _error("Use * for at most one index in a result path.", consumer)
        contract = reference_contract(value, steps, registry)
        paths = contract.referenceable_result_paths if contract else ()
        if not any(result_path_matches(value["path"], pattern) for pattern in paths):
            valid = ", ".join("/".join(path) for path in paths) or "none"
            return _error(
                f"Step {value['step']} does not expose result path {value['path']!r}. Valid paths: {valid}.",
                consumer or value["step"],
            )
    elif isinstance(value, (dict, list)):
        for item in (value.values() if isinstance(value, dict) else value):
            checked = _check_result_references(item, steps, registry, consumer)
            if not checked.ok:
                return checked
    return PlanCheck(True)


def _values(value: Any) -> set[str]:
    return {str(item) for item in value} if isinstance(value, list) else {str(value)}


def source_check(
    contract: Any, arguments: Mapping[str, Any],
    named: Mapping[str, set[str]],
    bound_kinds: frozenset[str] = frozenset(),
) -> tuple[str, tuple[str, ...]]:
    fields_by_kind: dict[str, list[str]] = {}
    for field, kind in contract.entity_fields:
        base = entity_kind(kind)
        fields_by_kind.setdefault(base, []).append(field)
        value = arguments.get(field)
        if value is None:
            continue
        selected = {str(item) for item in entity_values(value) if not has_reference(item)}
        if base in named and not selected <= named[base]:
            return ("The request named other sources of this kind. Offer these instead of reading them.",
                    tuple(sorted(selected - named[base])))
    for base, fields in fields_by_kind.items():
        if base in named and base not in bound_kinds and not any(field in arguments for field in fields):
            return "Filter this read to the named sources.", ()
    for _, result_kind in contract.result_entity_keys:
        base = entity_kind(result_kind)
        if base in named and base not in bound_kinds and base not in fields_by_kind:
            return "Use a capability that filters to the named sources.", ()
    return "", ()


def time_check(
    contract: Any, arguments: Mapping[str, Any], periods: tuple[tuple[str, Any, Any], ...],
    earlier: set[str],
) -> str:
    used = {field: arguments[field] for field in contract.time_fields if field in arguments}
    if not used:
        if contract.time_window and not contract.latest_fields:
            return "Provide both time boundaries for this read."
        return "Provide the time field for the selected period." if periods and contract.time_fields else ""
    ranges = [(start, end) for kind, start, end in periods if kind == "utc_range"]
    keys = {(field, str(value)) for kind, value, field in periods if kind == "key"}
    window_bounded = False
    if contract.time_window and any(field in used for field in contract.time_window[:2]):
        lower_field, upper_field, _ = contract.time_window
        if lower_field not in used or upper_field not in used:
            return "Provide both time boundaries for this read."
        if not any(has_reference(arguments[field]) for field in (lower_field, upper_field)):
            try:
                lower, upper = bound_time_window(contract, arguments)
            except CapabilityBindError as error:
                return str(error)
            if ranges and not any(start <= lower and upper <= end for start, end in ranges):
                return "The time window is outside the selected periods."
        window_bounded = True
    for field, value in used.items():
        if contract.time_window and field in contract.time_window[:2]:
            continue
        if field in contract.bounded_fields and window_bounded:
            continue
        if _reference(value, earlier):
            continue
        if field in contract.latest_fields:
            if periods and not window_bounded:
                return "A latest selector cannot accompany a selected period."
            continue
        if field in contract.bounded_fields:
            return "Keep this page selector inside a bounded time window."
        if keys and (field, str(value)) not in keys:
            return "The time value is outside the selected periods."
    return ""


@dataclass(frozen=True, slots=True)
class StepCheck(PlanCheck):
    tool: RegisteredAgentTool | None = None
    contract: CapabilityContract | None = None
    scope: Mapping[str, Any] | None = None


def check_step(
    step: Mapping[str, Any], registry: Mapping[str, RegisteredAgentTool],
    periods: tuple, named: Mapping[str, set[str]],
    earlier: set[str], *, resolved: bool = False,
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
    selected = original_tool(registry, capability, arguments) if capability in (READ_NAME, COMPARE_NAME) else None
    if selected is not None and unsupported_fields(selected, arguments):
        return _error(unsupported_field_error(selected, arguments), step_id)
    for reference in result_references(arguments):
        if reference.get("step") not in set(dependencies):
            return _error(
                f"Step {step_id} uses results of step {reference.get('step')!r}, which must be an "
                "earlier step in this plan listed in depends_on. Results of earlier plans cannot be "
                "referenced; read them again in this plan.", step_id,
            )
    if not valid_arguments(arguments, schema, set(dependencies)):
        return _error("Arguments must match the capability schema.", step_id)
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
    if contract is not None:
        if any(kind == "resolved" and owner == step_id for kind, owner, _ in periods) and any(
            field in arguments for field in (*contract.latest_fields, *contract.bounded_fields)
        ):
            return _error("Resolve the latest or current key without a page selector.", step_id)
        retained = any(field in arguments for field in contract.retained_fields)
        bound = frozenset(named) | frozenset(
            entity_kind(kind) for field, kind in contract.entity_fields if field in contract.retained_fields
        ) if retained else frozenset()
        issue, offered = source_check(contract, arguments, named, bound)
        if issue:
            return _error(issue, step_id, offered)
        step_periods = tuple(period for period in periods
                             if not (period[0] == "resolved" and period[1] == step_id))
        issue = time_check(contract, arguments, step_periods, earlier)
        if issue:
            return _error(issue, step_id)
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


def _check_steps(raw, registry, periods, named):
    earlier: set[str] = set()
    after_change: set[str] = set()
    steps_by_id = {}
    for step in raw["steps"]:
        if not isinstance(step, dict) or set(step) != {
            "id", "capability", "arguments", "reason", "depends_on",
        }:
            return _error("Each step needs id, capability, arguments, reason and depends_on.")
        step_id = step["id"]
        if not isinstance(step_id, str) or not 1 <= len(step_id) <= 40 or step_id in earlier:
            return _error("Give each step a unique short ID.")
        capability = step["capability"]
        if not isinstance(capability, str) or capability not in registry:
            return _error("Choose a registered capability.", step_id)
        dependencies = step["depends_on"]
        if not isinstance(dependencies, list) or any(dep not in earlier for dep in dependencies):
            return _error("Depend only on earlier steps.", step_id)
        classification = registry[capability].action_class
        depends_on_change = bool(after_change.intersection(dependencies))
        if classification is ActionClass.READ and depends_on_change:
            return _error(
                "Plan reads before changes; a read can't use a change's result.", step_id,
            )
        if depends_on_change or classification in (ActionClass.CHANGE, ActionClass.IRREVERSIBLE):
            after_change.add(step_id)
        if not isinstance(step["reason"], str) or not 1 <= len(step["reason"].strip()) <= 240:
            return _error("Give the step a short reason.", step_id)
        checked = check_step(step, registry, periods, named, earlier)
        if not checked.ok:
            return checked
        references_checked = _check_result_references(step["arguments"], steps_by_id, registry, step_id)
        if not references_checked.ok:
            return references_checked
        if checked.contract is not None:
            references_checked = check_entity_references(
                checked.contract, step["arguments"], steps_by_id, registry, step_id,
            )
            if not references_checked.ok:
                return references_checked
        earlier.add(step_id)
        steps_by_id[step_id] = step
    return earlier


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


def check_plan(
    raw: Any, registry: Mapping[str, RegisteredAgentTool],
    named_sources: Mapping[str, frozenset[Any]] | None = None,
) -> PlanCheck:
    """Return a fixable error for malformed or out-of-scope plans."""
    try:
        required = {"goal", "effort", "output", "steps"}
        if not isinstance(raw, dict) or not required <= set(raw) or set(raw) - required - {"periods", "entities"}:
            return _error("Supply goal, effort, output and steps; periods and entities are optional.")
        if not isinstance(raw["goal"], str) or not 1 <= len(raw["goal"].strip()) <= 240:
            return _error("Write a short goal.")
        if raw["effort"] not in ("low", "high", "max"):
            return _error("Choose low, high or max effort.")
        if raw["output"] not in output_forms(registry):
            return _error("Choose an available output form.")
        periods = parse_periods(raw.get("periods", []))
        key_fields = {field for name in registry if (contract := registry[name].contract) is not None
                      for field in contract.time_fields
                      if field not in (*contract.latest_fields, *contract.bounded_fields,
                                       *(contract.time_window[:2] if contract.time_window else ()))}
        if any(kind == "key" and field not in key_fields for kind, _, field in periods):
            return _error("Use a period key defined by a registered capability.")
        if not isinstance(raw.get("entities", []), list) or len(raw.get("entities", [])) > 32:
            return _error("List at most 32 entities.")
        contracts = [tool.contract for tool in registry.values()]
        contracts.extend(saved_report_contracts(registry).values())
        valid_kinds = {entity_kind(kind) for contract in contracts if contract
                       for _, kind in (*contract.entity_fields, *contract.result_entity_keys,
                                       *contract.result_path_kinds)}
        for index, entity in enumerate(raw.get("entities", []), 1):
            if not isinstance(entity, dict) or set(entity) != {"kind", "value"}:
                return _error(f"Entity {index} must be an object with exactly kind and value.")
            kind, value = entity["kind"], entity["value"]
            if not isinstance(kind, str) or not kind.strip():
                return _error(f"Entity {index} kind must be a non-empty string.")
            if entity_kind(kind) not in valid_kinds:
                return _error(
                    f"Entity {index} has unknown kind {kind!r}. Valid kinds: {', '.join(sorted(valid_kinds)) or 'none'}."
                )
            if isinstance(value, list) and not kind.endswith("_set"):
                return _error(f"Entity {index} of kind {kind} cannot use a list; use {kind}_set for IDs or references.")
            if isinstance(value, list) and not value:
                return _error(f"Entity {index} of kind {kind} needs a non-empty list of IDs or references.")
            for item_index, item in enumerate(entity_values(value), 1):
                if type(item) is int or isinstance(item, str) and item.strip():
                    continue
                elif (isinstance(item, dict) and isinstance(item.get("step"), str)
                      and item["step"] and _reference(item, {item["step"]})):
                    continue
                else:
                    return _error(
                        f"Entity {index} value {item_index} must be an ID, a non-empty name, "
                        "or a reference with step and path. Lists require a *_set kind.",
                    )
        if not isinstance(raw["steps"], list) or not 1 <= len(raw["steps"]) <= 48:
            return _error("List between 1 and 48 steps.")
        named = {entity_kind(kind): {str(item) for item in values}
                 for kind, values in (named_sources or {}).items() if values}
        checked = _check_steps(raw, registry, periods, named)
        if isinstance(checked, PlanCheck):
            return checked
        earlier = checked
        steps_by_id = {step["id"]: step for step in raw["steps"]}
        for index, entity in enumerate(raw.get("entities", []), 1):
            for reference in result_references(entity["value"]):
                if not _reference(reference, earlier):
                    return _error(f"Entity {index} must reference a planned step and result path.")
                checked = _check_result_references(reference, steps_by_id, registry)
                if not checked.ok:
                    return _error(f"Entity {index}: {checked.error}", checked.step_id)
        if _mixes_irreversible_changes(raw["steps"], registry):
            return _error("Only irreversible changes of the same kind may share a preview.")
        for kind, step_id, path in periods:
            if kind != "resolved":
                continue
            if step_id not in earlier:
                return _error("Resolve each period with a planned step.")
            checked = _check_result_references({"step": step_id, "path": path}, steps_by_id, registry)
            if not checked.ok:
                return checked
            resolver = registry[steps_by_id[step_id]["capability"]].contract
            if resolver is None or not any(
                tuple(path) == pattern for pattern in resolver.period_results
            ):
                return _error("Resolve the period through an owning capability's period result.")
        if raw["output"] != "text" and not any(
            step["capability"] == raw["output"] for step in raw["steps"]
        ):
            return _error("Include the selected output capability in the steps.")
        return PlanCheck(True)
    except ValueError as error:
        return _error(str(error))
    except Exception:
        LOGGER.exception("Agent plan check failed unexpectedly")
        return _error("Correct the plan fields and values.")
