"""Compact shapes of serialized capability results, including every record field."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from types import UnionType
from typing import Union, get_args, get_origin, get_type_hints, is_typeddict


def names(value: str) -> dict:
    return dict.fromkeys(value.split(","), "")


def result_fields(result_type) -> dict | list | str:
    """Derive dataclass and TypedDict fields as serialized, including nested records."""
    origin, arguments = get_origin(result_type), get_args(result_type)
    if origin in (Union, UnionType):
        choices = [result_fields(item) for item in arguments if item is not type(None)]
        return next((choice for choice in choices if choice), "")
    if origin in (tuple, list):
        return [result_fields(arguments[0])] if arguments else [""]
    if origin in (dict, Mapping):
        return "{*}"
    if is_dataclass(result_type) or is_typeddict(result_type):
        hints = get_type_hints(result_type)
        keys = (field.name for field in fields(result_type)) if is_dataclass(result_type) else hints
        return {key: result_fields(hints[key]) for key in keys}
    return ""


def format_returns(shape: dict) -> str:
    def detail(value):
        if isinstance(value, dict):
            return "{" + format_returns(value) + "}"
        if isinstance(value, list):
            return "[" + detail(value[0]) + "]"
        return value

    return ",".join(key + (":" + detail(value) if value else "")
                    for key, value in shape.items())
