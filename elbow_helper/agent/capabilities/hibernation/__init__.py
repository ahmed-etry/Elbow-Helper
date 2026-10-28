"""Hibernation agent capabilities."""

from .reads import hibernation_tools
from .tickets import reactivation_ticket_tools
from .commands import hibernation_adapters


TOOLS = (
    *hibernation_tools(),
    *reactivation_ticket_tools(),
)

COMMAND_ADAPTERS = (
    *hibernation_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]
