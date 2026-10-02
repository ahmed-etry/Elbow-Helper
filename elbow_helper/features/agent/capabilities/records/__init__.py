"""Records agent capabilities."""

from .reads import record_tools
from .commands import record_adapters

TOOLS = (
    *record_tools(),
)

COMMAND_ADAPTERS = (
    *record_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]
