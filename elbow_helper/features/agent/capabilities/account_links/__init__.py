"""Account links agent capabilities."""

from .suggestions import account_suggestion_tools
from .reads import member_tools
from .role_audit import role_tools
from .commands import account_link_adapters

TOOLS = (
    *account_suggestion_tools(),
    *member_tools(),
    *role_tools(),
)

COMMAND_ADAPTERS = (
    *account_link_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]
