"""Wars agent capabilities."""

from .reads import war_tools
from .statements import war_statement_adapters

TOOLS = (
    *war_tools(),
)

COMMAND_ADAPTERS = (
    *war_statement_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]
