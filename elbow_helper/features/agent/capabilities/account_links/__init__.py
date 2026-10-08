"""Account links agent capabilities."""

from .suggestions import account_suggestion_tools
from .commands import account_link_adapters
from ...actions.undo import merge_undo_handlers
from .commands import UNDO_HANDLERS as COMMANDS_UNDO_HANDLERS


TOOLS = (
    *account_suggestion_tools(),
)

COMMAND_ADAPTERS = (
    *account_link_adapters(),
)


UNDO_HANDLERS = merge_undo_handlers(
    COMMANDS_UNDO_HANDLERS,
)

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]
