"""Collect recovery handlers exported by the actions that own them."""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .contracts import PreparedAction


UndoHandler = Callable[[Any, Mapping[str, Any]], Awaitable[PreparedAction]]


def merge_undo_handlers(*groups: Mapping[str, UndoHandler]) -> dict[str, UndoHandler]:
    handlers = {}
    for group in groups:
        for path, handler in group.items():
            if path in handlers:
                raise ValueError(f"Duplicate undo handler: {path}")
            handlers[path] = handler
    return handlers
