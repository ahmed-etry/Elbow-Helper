"""Display preview values using the feature's visible state labels."""

from datetime import datetime
from typing import Any

from ..wording import (
    ACTION_VALUE_HIDDEN, ACTION_VALUE_NO, ACTION_VALUE_NOT_SET,
    ACTION_VALUE_VISIBLE, ACTION_VALUE_YES,
)


def display_value(value: Any, *, visibility: bool = False) -> str:
    if value is None or value == "":
        return ACTION_VALUE_NOT_SET
    if isinstance(value, datetime):
        return f"<t:{int(value.timestamp())}:f>"
    if isinstance(value, bool):
        return (ACTION_VALUE_VISIBLE if value else ACTION_VALUE_HIDDEN) if visibility else (
            ACTION_VALUE_YES if value else ACTION_VALUE_NO)
    if isinstance(value, (list, tuple)):
        return ", ".join(display_value(item) for item in value) or ACTION_VALUE_NOT_SET
    return str(value)
