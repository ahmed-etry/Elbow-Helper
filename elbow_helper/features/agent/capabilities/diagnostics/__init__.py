"""Diagnostics agent capabilities."""

from .commands import diagnostic_adapters

TOOLS = ()

COMMAND_ADAPTERS = (
    *diagnostic_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]
