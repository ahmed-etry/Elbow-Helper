"""Resolve an action argument against earlier confirmed results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .checker import _valid_value
from .arguments import takes_single_value
from .results import is_record_table
from .reference_paths import path_key, valid_path


class UnresolvedReferenceError(KeyError):
    """Keep the failed reference identity without retaining result values."""

    def __init__(self, step_id, path):
        super().__init__("Unresolved earlier result reference")
        self.step_id = step_id
        self.path = path


class SingleValueReferenceError(ValueError):
    """A collection cannot supply an unambiguous single argument value."""


class MalformedReferenceError(ValueError):
    """A reference-like argument did not use the reference object shape."""


def reference_shape_issue(value, detail, field="arguments", *, array_item=False):
    if detail.get("x-result-list"):
        return None
    kind = detail.get("type")
    scalar = kind in ("string", "integer", "number", "boolean")
    malformed = (
        isinstance(value, str) and value.startswith("$ref:") and (array_item or kind == "array")
        or isinstance(value, dict) and set(value) != {"step", "path"}
        and bool(set(value) & {"step", "path", "$ref"}) and (scalar or kind == "array")
    )
    if malformed:
        return field, (f'{field} must use a result reference with exactly "step" and "path": '
                       '{"step": "<id>", "path": [...]}.')
    if isinstance(value, dict) and set(value) != {"step", "path"}:
        for name, item in value.items():
            if issue := reference_shape_issue(item, detail.get("properties", {}).get(name, {}),
                                             name if field == "arguments" else f"{field}.{name}"):
                return issue
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if issue := reference_shape_issue(item, detail.get("items", {}), f"{field}[{index}]",
                                             array_item=kind == "array"):
                return issue
    return None


def _walk(source: Any, path: list[Any]) -> Any:
    if not valid_path(path):
        raise TypeError("Invalid result path")
    return _walk_parts(source, path)


def _walk_parts(source: Any, path: list[Any]) -> Any:
    for index, part in enumerate(path):
        if part == "rows" and is_record_table(source):
            continue
        if part == "*":
            if not isinstance(source, list):
                raise TypeError("Only a list can be expanded")
            rest = path[index + 1:]
            collected = [_walk_parts(item, rest) for item in source]
            return [value for group in collected for value in group] if "*" in rest else collected
        source = source[path_key(source, part)]
    return source


def _scalar_list(detail: Mapping[str, Any]) -> bool:
    kind, items = detail.get("type"), detail.get("items")
    return (kind == "array" or isinstance(kind, list) and "array" in kind) and isinstance(items, Mapping) \
        and items.get("type") in ("string", "integer", "number", "boolean")


def _flatten_scalar_list(values: Any, detail: Mapping[str, Any]) -> Any:
    """A reference through a list-valued field yields one list per record; an
    array-of-scalars argument takes them as one list."""
    if (not isinstance(values, list) or not _scalar_list(detail)
            or not any(isinstance(item, list) for item in values)):
        return values
    flat = [value for item in values for value in (item if isinstance(item, list) else [item])]
    if detail.get("uniqueItems"):
        flat = [value for index, value in enumerate(flat) if value not in flat[:index]]
    return flat


def resolve_arguments(
    arguments: Mapping[str, Any], results: Mapping[str, Mapping[str, Any]],
    *, schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if issue := reference_shape_issue(arguments, schema or {}):
        raise MalformedReferenceError(issue[1])
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
            kinds = detail.get('type')
            if ((kinds == 'array' or isinstance(kinds, list) and 'array' in kinds)
                    and isinstance(collected, (str, int, float, bool)) and 'items' in detail
                    and _valid_value(collected, detail['items'])):
                return [collected]
            return _flatten_scalar_list(collected, detail)
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
            if any(isinstance(item, dict) and set(item) == {"step", "path"} for item in value):
                return _flatten_scalar_list(items, detail)
            return items
        return value
    return resolve(dict(arguments), schema or {})
