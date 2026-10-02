"""Events agent capabilities."""

from .reads import event_tools
from .management import event_management_tools
from .commands import event_adapters

TOOLS = (
    *event_tools(),
    *event_management_tools(),
)

COMMAND_ADAPTERS = (
    *event_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]
