"""Wars agent capabilities."""

from .statements import war_statement_adapters


TOOLS = (
)

COMMAND_ADAPTERS = (
    *war_statement_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]
