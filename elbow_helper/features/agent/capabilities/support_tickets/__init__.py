"""Support tickets agent capabilities."""

from .reads import support_tools
from .reopen import support_reopen_tools
from .commands import support_ticket_adapters


TOOLS = (
    *support_tools(),
    *support_reopen_tools(),
)

COMMAND_ADAPTERS = (
    *support_ticket_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]
