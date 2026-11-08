"""Small number readers for values that arrive as saved JSON or Discord data."""

from __future__ import annotations

from typing import Any


def positive_int_or_none(value: Any) -> int | None:
    """An int above zero, or a string of digits that makes one; anything else is None."""

    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def non_negative_int(value: Any) -> int:
    """The value as an int, with anything missing, unreadable or negative as 0."""

    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0
