"""Records agent capabilities."""

from .commands import record_adapters
from ...actions.undo import merge_undo_handlers
from .commands import UNDO_HANDLERS as COMMANDS_UNDO_HANDLERS


TOOLS = (
)

COMMAND_ADAPTERS = (
    *record_adapters(),
)


UNDO_HANDLERS = merge_undo_handlers(
    COMMANDS_UNDO_HANDLERS,
)

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]
