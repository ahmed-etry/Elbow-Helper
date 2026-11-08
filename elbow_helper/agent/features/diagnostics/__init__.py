"""Diagnostics agent capabilities."""

from elbow_helper.agent.features.diagnostics.commands import diagnostic_adapters


TOOLS = ()

COMMAND_ADAPTERS = (
    *diagnostic_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]
