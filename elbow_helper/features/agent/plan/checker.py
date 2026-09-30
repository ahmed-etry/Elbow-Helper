"""Check a plan against capability schemas and declared scope."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from collections.abc import Mapping
from typing import Any

from ..models import RegisteredAgentTool
from ..actions.contracts import ActionClass
from ..capabilities import CONTRACTS, SAVED_REPORT_CONTRACTS, CapabilityBindError, bound_time_window, compile_capability_call, entity_kind
from ..tools.saved_reports import COMPARE_NAME, READ_NAME, original_arguments, original_tool
from .format import output_forms


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


def _periods(raw: Any) -> tuple[tuple[str, Any, Any], ...]:
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
        return _valid_arguments(value, schema, dependencies)
    return False


def _valid_arguments(arguments: Any, schema: Mapping[str, Any], dependencies: set[str] | None = None) -> bool:
    if not isinstance(arguments, dict):
        return False
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        return False
    if set(arguments) - set(properties) or set(schema.get("required", ())) - set(arguments):
        return False
    return all(_valid_value(value, properties[field], dependencies) for field, value in arguments.items())


def _has_reference(value: Any) -> bool:
    if isinstance(value, dict):
        return set(value) == {"step", "path"} or any(_has_reference(item) for item in value.values())
    return isinstance(value, list) and any(_has_reference(item) for item in value)


def _reference(value: Any, earlier: set[str]) -> bool:
    return (isinstance(value, dict) and set(value) == {"step", "path"}
            and isinstance(value["step"], str) and value["step"] in earlier and isinstance(value["path"], list)
            and 1 <= len(value["path"]) <= 8
            and all(isinstance(part, str) and part or type(part) is int and part >= 0
                    for part in value["path"]))


def _kind(kind: str) -> str:
    return entity_kind(kind)


def _values(value: Any) -> set[str]:
    return {str(item) for item in value} if isinstance(value, list) else {str(value)}


def _source_check(
    contract: Any, arguments: Mapping[str, Any],
    named: Mapping[str, set[str]], entities: Mapping[str, set[str]],
    references: Mapping[str, Any],
    bound_kinds: frozenset[str] = frozenset(),
) -> tuple[str, tuple[str, ...]]:
    fields_by_kind: dict[str, list[str]] = {}
    for field, kind in contract.entity_fields:
        base = _kind(kind)
        fields_by_kind.setdefault(base, []).append(field)
        value = arguments.get(field)
        if value is None or field in references:
            continue
        selected = _values(value)
        if base in named and not selected <= named[base]:
            return ("The request named other sources of this kind. Offer these instead of reading them.",
                    tuple(sorted(selected - named[base])))
        if base not in bound_kinds and not selected <= entities.get(base, set()):
            return "Declare this entity in the plan.", ()
    for base, fields in fields_by_kind.items():
        if base in named and base not in bound_kinds and not any(field in arguments for field in fields):
            return "Filter this read to the named sources.", ()
    for _, result_kind in contract.result_entity_keys:
        base = _kind(result_kind)
        if base in named and base not in bound_kinds and base not in fields_by_kind:
            return "Use a capability that filters to the named sources.", ()
    return "", ()


def _time_check(
    contract: Any, arguments: Mapping[str, Any], periods: tuple[tuple[str, Any, Any], ...],
    earlier: set[str],
) -> str:
    used = {field: arguments[field] for field in contract.time_fields if field in arguments}
    if not used:
        if contract.time_window and not contract.latest_fields:
            return "Declare a period and both time boundaries for this read."
        return "Declare the time field that bounds this read." if periods and contract.time_fields else ""
    if not periods:
        if set(used) <= set(contract.latest_fields):
            return ""
        return "Declare the period before using a time field."
    ranges = [(start, end) for kind, start, end in periods if kind == "utc_range"]
    keys = {(field, str(value)) for kind, value, field in periods if kind == "key"}
    resolved = {(step, tuple(path)) for kind, step, path in periods if kind == "resolved"}
    window_bounded = False
    if contract.time_window and any(field in used for field in contract.time_window[:2]):
        lower_field, upper_field, _ = contract.time_window
        if lower_field not in used or upper_field not in used:
            return "Provide both time boundaries to keep the read inside the period."
        if any(_reference(arguments[field], earlier) for field in (lower_field, upper_field)):
            if not all(_reference(arguments[field], earlier)
                       and (arguments[field]["step"], tuple(arguments[field]["path"])) in resolved for field in (lower_field, upper_field)):
                return "Bind both time boundaries to a declared resolved period."
        else:
            try:
                lower, upper = bound_time_window(contract, arguments)
            except CapabilityBindError as error:
                return str(error)
            if not any(start <= lower and upper <= end for start, end in ranges):
                return "The time window is outside the declared periods."
        window_bounded = True
    for field, value in used.items():
        if contract.time_window and field in contract.time_window[:2]:
            continue
        if field in contract.bounded_fields and window_bounded:
            continue
        if _reference(value, earlier):
            if (value["step"], tuple(value["path"])) not in resolved:
                return "Bind this time value to a declared resolved period."
            continue
        if field in contract.latest_fields:
            return "A latest selector cannot accompany a declared period."
        if field in contract.bounded_fields:
            return "Keep this page selector inside a bounded time window."
        if (field, str(value)) in keys:
            continue
        return "The time value is outside the declared periods."
    return ""


def check_plan(
    raw: Any, registry: Mapping[str, RegisteredAgentTool],
    named_sources: Mapping[str, frozenset[Any]] | None = None,
) -> PlanCheck:
    """Return a fixable error for malformed or out-of-scope plans."""
    try:
        if not isinstance(raw, dict) or set(raw) != {
            "goal", "effort", "output", "periods", "entities", "steps",
        }:
            return _error("Supply goal, effort, output, periods, entities and steps.")
        if not isinstance(raw["goal"], str) or not 1 <= len(raw["goal"].strip()) <= 240:
            return _error("Write a short goal.")
        if raw["effort"] not in ("low", "high"):
            return _error("Choose low or high effort.")
        if raw["output"] not in output_forms(registry):
            return _error("Choose an available output form.")
        periods = _periods(raw["periods"])
        key_fields = {field for name in registry if (contract := CONTRACTS.get(name)) is not None
                      for field in contract.time_fields
                      if field not in (*contract.latest_fields, *contract.bounded_fields,
                                       *(contract.time_window[:2] if contract.time_window else ()))}
        if any(kind == "key" and field not in key_fields for kind, _, field in periods):
            return _error("Use a period key defined by a registered capability.")
        if not isinstance(raw["entities"], list) or len(raw["entities"]) > 32:
            return _error("List at most 32 entities.")
        entities: dict[str, set[str]] = {}
        for entity in raw["entities"]:
            if not isinstance(entity, dict) or set(entity) != {"kind", "value"}:
                return _error("Each entity needs a kind and an ID or name.")
            kind, value = entity["kind"], entity["value"]
            if not isinstance(kind, str) or not kind or (type(value) not in (str, int)
                    and not (isinstance(value, dict) and set(value) == {"step", "path"})):
                return _error("Each entity needs a kind and an ID or name.")
            entities.setdefault(_kind(kind), set()).add(str(value))
        if not isinstance(raw["steps"], list) or not 1 <= len(raw["steps"]) <= 48:
            return _error("List between 1 and 48 steps.")
        named = {_kind(kind): {str(item) for item in values}
                 for kind, values in (named_sources or {}).items() if values}
        for kind, values in named.items():
            if not values <= entities.get(kind, set()):
                return _error("Declare each named source in the plan entities.")
        earlier: set[str] = set()
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
            if not isinstance(step["reason"], str) or not 1 <= len(step["reason"].strip()) <= 240:
                return _error("Give the step a short reason.", step_id)
            tool = registry[capability]
            arguments = step["arguments"]
            if not isinstance(arguments, dict):
                return _error("Use an argument object.", step_id)
            references = {field: value for field, value in arguments.items()
                          if _has_reference(value)}
            schema = tool.definition.parameters
            if not _valid_arguments(arguments, schema, set(dependencies)):
                return _error("Arguments must match the capability schema.", step_id)
            selected = original_tool(capability, arguments) if capability in (READ_NAME, COMPARE_NAME) else None
            if capability in (READ_NAME, COMPARE_NAME):
                if selected is None or not _valid_arguments(
                    original_arguments(arguments), selected.definition.parameters,
                    set(dependencies),
                ):
                    return _error("Use the fields supported by this report kind.", step_id)
            contract = (SAVED_REPORT_CONTRACTS[selected.definition.name] if selected is not None
                        else CONTRACTS.get(capability))
            if contract is not None:
                if any(kind == "resolved" and owner == step_id for kind, owner, _ in periods) and any(
                    field in arguments for field in (*contract.latest_fields, *contract.bounded_fields)
                ):
                    return _error("Resolve the latest or current key without a page selector.", step_id)
                retained = any(field in arguments for field in contract.retained_fields)
                bound = frozenset(named) | frozenset(
                    _kind(kind) for field, kind in contract.entity_fields if field in contract.retained_fields
                ) if retained else frozenset()
                issue, offered = _source_check(contract, arguments, named, entities, references,
                                              bound)
                if issue:
                    return _error(issue, step_id, offered)
                step_periods = tuple(period for period in periods
                                     if not (period[0] == "resolved" and period[1] == step_id))
                issue = _time_check(contract, arguments, step_periods, earlier)
                if issue:
                    return _error(issue, step_id)
                if not references:
                    try:
                        compile_capability_call(
                            selected or tool,
                            original_arguments(arguments) if selected else arguments,
                            contract=contract,
                        )
                    except CapabilityBindError as error:
                        return _error(str(error), step_id)
            earlier.add(step_id)
        for entity in raw["entities"]:
            if isinstance(entity["value"], dict) and not _reference(entity["value"], earlier):
                return _error("Resolve each entity with a planned step and result path.")
        changes = [step for step in raw["steps"]
                   if registry[step["capability"]].action_class in (
                       ActionClass.CHANGE, ActionClass.IRREVERSIBLE,
                   )]
        if len(changes) > 1 and any(
            registry[step["capability"]].action_class is ActionClass.IRREVERSIBLE
            for step in changes
        ):
            return _error("Confirm an irreversible change on its own.")
        steps_by_id = {step["id"]: step for step in raw["steps"]}
        for kind, step_id, path in periods:
            if kind != "resolved":
                continue
            if step_id not in earlier:
                return _error("Resolve each period with a planned step.")
            resolver = CONTRACTS.get(steps_by_id[step_id]["capability"])
            if resolver is None or not any(
                tuple(path) == pattern for pattern in resolver.period_results
            ):
                return _error("Resolve the period through an owning capability's period result.")
        if raw["output"] != "text" and not any(
            step["capability"] == raw["output"] for step in raw["steps"]
        ):
            return _error("Include the selected output capability in the steps.")
        return PlanCheck(True)
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError):
        return _error("Correct the plan fields and values.")
